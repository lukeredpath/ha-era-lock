"""Intelligent wake coordination for the ERA Protect Lock.

The BLE lock buffers DP events and only reports them when it next wakes. Forcing
a wake (dp54 synch) fetches buffered events promptly but costs battery, so we
wake only at worthwhile moments:
  - after an unlock, POLL for the real auto-lock at WAKE_POLL_OFFSETS (30/40/50s)
    so we capture the device's own dp11 lock signal; the polls stop as soon as
    the lock reports locked (``cancel_polls``). The lock's failsafe relock timer
    (>=60s) only fires if none of the polls captured it;
  - when a configured door contact opens or closes;
  - on an idle heartbeat (no contact for a configurable interval; 0 = off);
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
POST_CLOSE_SYNC_S = 60  # after a thumbturn-exit close, re-sync to catch a lock-up
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
        self._followup_unsub = None

    @callback
    def start(self) -> None:
        if self._heartbeat_s and self._heartbeat_s > 0:
            self._heartbeat_unsub = async_track_time_interval(
                self.hass, self._check_idle, timedelta(seconds=60))

    @callback
    def stop(self) -> None:
        self.cancel_polls()
        self._cancel_followup()
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
    def schedule_followup(self, delay: float = POST_CLOSE_SYNC_S) -> None:
        """Schedule a single delayed wake. Used after a thumbturn-exit door close
        to catch a lock-up done shortly after the door shut (which the immediate
        close wake is too early to see)."""
        self._cancel_followup()

        @callback
        def _fire(_now) -> None:
            self._followup_unsub = None
            self._wake("post-close re-sync", force=True)

        self._followup_unsub = async_call_later(self.hass, delay, _fire)

    @callback
    def cancel_followup(self) -> None:
        """Drop a pending post-close re-sync (a live lock-up was already seen)."""
        self._cancel_followup()

    @callback
    def _cancel_followup(self) -> None:
        if self._followup_unsub is not None:
            self._followup_unsub()
            self._followup_unsub = None

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
        if time.monotonic() - self._last_inbound >= self._heartbeat_s:
            self._wake("idle heartbeat")

    def _wake(self, reason: str, force: bool = False) -> None:
        now = time.monotonic()
        since = now - self._last_wake
        if not force and since < MIN_WAKE_GAP:
            _LOGGER.debug("wake suppressed (rate limit): %s", reason)
            return
        if force and since < FORCED_WAKE_COALESCE_S:
            # e.g. the +60s post-close re-sync and a +30s relock poll landing
            # together: the wake already in flight flushes the same data.
            _LOGGER.debug("wake merged into one %.1fs ago: %s", since, reason)
            return
        self._last_wake = now
        _LOGGER.debug("wake: %s", reason)
        self.hass.async_add_executor_job(self._listener.wake)
