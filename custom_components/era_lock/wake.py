"""Intelligent wake coordination for the ERA Protect Lock.

The BLE lock buffers DP events and only reports them when it next wakes. Forcing
a wake (dp54 synch) fetches buffered events promptly but costs battery, so we
wake only at worthwhile moments:
  - after an unlock, POLL for the real auto-lock at WAKE_POLL_OFFSETS (30/40/50s)
    so we capture the device's own dp11 lock signal; the polls stop as soon as
    the lock reports locked (``cancel_polls``). The lock's failsafe relock timer
    (>=60s) only fires if none of the polls captured it;
  - when a configured door contact opens or closes;
  - after ANY door close while the lock isn't known to be locked, a short
    series at CLOSE_WAKE_OFFSETS (+10/+20/+30s) to catch a handle-lift (which
    the immediate close wake is too early to see); the series stops as soon as
    a device lock settles or the door reopens (``cancel_close_wakes``);
  - once, mid-way through a door hold, if the first flush brought nothing;
  - once after setup connects (``startup_sync``): the state is unknown after a
    restart and the lock may hold buffered events;
  - on an idle heartbeat (no lock report — or wake — for a configurable
    interval; 0 = off);
  - plus the Sync button.
Non-poll wakes are rate-limited (MIN_WAKE_GAP) for battery. Forced wakes bypass
that limit but are still coalesced: one sent within FORCED_WAKE_COALESCE_S of the
previous wake is merged into it (see _wake).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

_LOGGER = logging.getLogger(__name__)

MIN_WAKE_GAP = 20.0  # minimum seconds between non-poll forced wakes (battery)
WAKE_POLL_OFFSETS = (30, 40, 50)  # seconds after an unlock to poll for auto-lock
# After a door close (state unlocked/unknown, or a door hold pending), forced
# wakes at these offsets to catch a handle-lift. 10s apart: well past the 5s
# coalescing window, so they never merge with each other.
CLOSE_WAKE_OFFSETS = (10, 20, 30)
# A forced wake within this many seconds of the previous wake is merged into it.
# A dp54 synch makes the lock reconnect and flush within ~3s, and it then stays
# connected (reporting live) for a while, so a second synch inside that window
# can't fetch anything the first won't. 5s covers the flush with headroom, yet is
# well under the 10s spacing of the relock polls, so the polls never merge with
# each other — only with an unrelated wake that has just done the same job.
FORCED_WAKE_COALESCE_S = 5.0


class EraWakeManager:
    """Decides when to force the lock to reconnect, rate-limited for battery."""

    def __init__(self, hass: HomeAssistant, listener, *, heartbeat_s: int) -> None:
        self.hass = hass
        self._listener = listener
        self._heartbeat_s = heartbeat_s
        self._last_inbound = time.monotonic()
        self._last_wake = float("-inf")  # no wake sent yet
        self._heartbeat_unsub = None
        self._poll_unsubs: list = []
        self._close_unsubs: list = []
        self.stats: dict = {"sent": 0, "merged": 0, "rate_limited": 0,
                            "last_reason": None, "last_wake": None}

    @callback
    def start(self) -> None:
        if self._heartbeat_s and self._heartbeat_s > 0:
            self._heartbeat_unsub = async_track_time_interval(
                self.hass, self._check_idle, timedelta(seconds=60))

    @property
    def heartbeat_s(self) -> int:
        return self._heartbeat_s

    @callback
    def stop(self) -> None:
        self.cancel_polls()
        self.cancel_close_wakes()
        if self._heartbeat_unsub:
            self._heartbeat_unsub()
            self._heartbeat_unsub = None

    def note_inbound(self) -> None:
        """Record lock activity (called from the paho thread; bare assignment)."""
        self._last_inbound = time.monotonic()

    # -- triggers (called from the HA loop) -----------------------------------
    @callback
    def on_unlock(self) -> None:
        """Poll for the real auto-lock after an unlock; cancel_polls() when the
        lock reports locked."""
        self.cancel_polls()
        for offset in WAKE_POLL_OFFSETS:
            # The scheduled action MUST be a @callback: a plain function makes
            # async_call_later dispatch it to an executor thread, where _wake's
            # loop-only async_add_executor_job raises "no running event loop".
            @callback
            def _poll(_now, o=offset) -> None:
                self._wake(f"relock poll +{o}s", force=True)

            self._poll_unsubs.append(async_call_later(self.hass, offset, _poll))

    @callback
    def cancel_polls(self) -> None:
        for unsub in self._poll_unsubs:
            unsub()
        self._poll_unsubs.clear()

    @callback
    def on_door_change(self, force: bool = False) -> None:
        """Wake on a door change. ``force`` bypasses the rate limit — used for a
        door opening while the lock is believed locked, whose unlock attribution
        is held waiting for the buffered flush this wake triggers."""
        self._wake("door event", force=force)

    @callback
    def schedule_close_wakes(self, elapsed: float = 0.0) -> None:
        """Start the post-close series (CLOSE_WAKE_OFFSETS from the close),
        replacing any running one. ``elapsed``: seconds since the close
        already passed (offsets already behind us are skipped)."""
        self.cancel_close_wakes()
        for offset in CLOSE_WAKE_OFFSETS:
            delay = offset - elapsed
            if delay <= 0:
                continue

            @callback
            def _fire(_now, o=offset) -> None:
                self._wake(f"post-close +{o}s", force=True)

            self._close_unsubs.append(async_call_later(self.hass, delay, _fire))

    @callback
    def cancel_close_wakes(self) -> None:
        """Drop the rest of the post-close series (a device lock settled, or
        the door reopened)."""
        for unsub in self._close_unsubs:
            unsub()
        self._close_unsubs.clear()

    @property
    def close_wakes_pending(self) -> bool:
        return bool(self._close_unsubs)

    @callback
    def hold_resync(self) -> None:
        """A door hold has seen nothing yet: one more forced sync in case the
        first wake's flush was missed."""
        self._wake("door hold re-sync", force=True)

    @callback
    def startup_sync(self) -> None:
        """After setup connects: the state is unknown and the lock may hold
        buffered events — one forced sync."""
        self._wake("startup sync", force=True)

    @callback
    def manual_sync(self) -> None:
        """The Sync button: a person asked, so bypass the battery rate limit
        (still coalesced with a wake sent moments ago)."""
        self._wake("sync button", force=True)

    @callback
    def on_reconnect(self) -> None:
        """The hub reports the lock reachable again after being offline: sync
        once to pick up anything it reports on reconnect."""
        self._wake("lock back online", force=True)

    @callback
    def request_wake(self, reason: str = "manual") -> None:
        self._wake(reason)

    # -- internals ------------------------------------------------------------
    @callback
    def _check_idle(self, _now) -> None:
        # Measured from the last lock report OR wake: a lock that doesn't answer
        # (asleep out of range, link down) is retried once per interval, not on
        # every 60s tick.
        last = max(self._last_inbound, self._last_wake)
        if time.monotonic() - last >= self._heartbeat_s:
            self._wake("idle heartbeat")

    def _wake(self, reason: str, force: bool = False) -> None:
        now = time.monotonic()
        since = now - self._last_wake
        if not force and since < MIN_WAKE_GAP:
            self.stats["rate_limited"] += 1
            _LOGGER.debug("wake suppressed (rate limit): %s", reason)
            return
        if force and since < FORCED_WAKE_COALESCE_S:
            # e.g. a post-close wake and a +30s relock poll landing together:
            # the wake already in flight flushes the same data.
            self.stats["merged"] += 1
            _LOGGER.debug("wake merged into one %.1fs ago: %s", since, reason)
            return
        self._last_wake = now
        self.stats["sent"] += 1
        self.stats["last_reason"] = reason
        self.stats["last_wake"] = time.time()
        _LOGGER.debug("wake: %s", reason)
        self.hass.async_add_executor_job(self._listener.wake)
