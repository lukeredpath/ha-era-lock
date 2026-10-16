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
Non-poll wakes are rate-limited (MIN_WAKE_GAP) for battery.
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


class EraWakeManager:
    """Decides when to force the lock to reconnect, rate-limited for battery."""

    def __init__(self, hass: HomeAssistant, listener, *, heartbeat_s: int) -> None:
        self.hass = hass
        self._listener = listener
        self._heartbeat_s = heartbeat_s
        self._last_inbound = time.monotonic()
        self._last_wake = 0.0
        self._heartbeat_unsub = None
        self._poll_unsubs: list = []

    @callback
    def start(self) -> None:
        if self._heartbeat_s and self._heartbeat_s > 0:
            self._heartbeat_unsub = async_track_time_interval(
                self.hass, self._check_idle, timedelta(seconds=60))

    @callback
    def stop(self) -> None:
        self.cancel_polls()
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
            self._poll_unsubs.append(async_call_later(
                self.hass, offset,
                lambda _now, o=offset: self._wake(f"relock poll +{o}s", force=True)))

    @callback
    def cancel_polls(self) -> None:
        for unsub in self._poll_unsubs:
            unsub()
        self._poll_unsubs.clear()

    @callback
    def on_door_change(self) -> None:
        self._wake("door event")

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
        if not force and now - self._last_wake < MIN_WAKE_GAP:
            _LOGGER.debug("wake suppressed (rate limit): %s", reason)
            return
        self._last_wake = now
        _LOGGER.debug("wake: %s", reason)
        self.hass.async_add_executor_job(self._listener.wake)
