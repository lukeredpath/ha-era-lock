"""Intelligent wake coordination for the ERA Protect Lock.

The BLE lock buffers DP events and only reports them when it next wakes. Forcing
a wake (dp54 synch) fetches buffered events promptly but costs battery, so we
wake only at worthwhile moments, all gated through a rate limiter:
  - shortly after an unlock, to confirm the real relock state;
  - when a configured door contact opens or closes (a likely state change);
  - on an idle heartbeat (no contact for a configurable interval; 0 = off);
  - plus the manual/automatable Sync button.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

_LOGGER = logging.getLogger(__name__)

MIN_WAKE_GAP = 20.0  # minimum seconds between forced wakes (battery guard)


class EraWakeManager:
    """Decides when to force the lock to reconnect, rate-limited for battery."""

    def __init__(self, hass: HomeAssistant, listener, *,
                 heartbeat_s: int, relock_s: int) -> None:
        self.hass = hass
        self._listener = listener
        self._heartbeat_s = heartbeat_s
        self._relock_s = relock_s
        self._last_inbound = time.monotonic()
        self._last_wake = 0.0
        self._heartbeat_unsub = None
        self._relock_unsub = None

    @callback
    def start(self) -> None:
        if self._heartbeat_s and self._heartbeat_s > 0:
            self._heartbeat_unsub = async_track_time_interval(
                self.hass, self._check_idle, timedelta(seconds=60))

    @callback
    def stop(self) -> None:
        for unsub in (self._heartbeat_unsub, self._relock_unsub):
            if unsub:
                unsub()
        self._heartbeat_unsub = self._relock_unsub = None

    def note_inbound(self) -> None:
        """Record lock activity (called from the paho thread; bare assignment)."""
        self._last_inbound = time.monotonic()

    # -- triggers (all called from the HA loop) -------------------------------
    @callback
    def on_unlock(self) -> None:
        """After an unlock, wake once just past the relock window to confirm."""
        if self._relock_unsub:
            self._relock_unsub()
        delay = (self._relock_s or 30) + 5
        self._relock_unsub = async_call_later(self.hass, delay, self._relock_check)

    @callback
    def on_door_change(self) -> None:
        self._wake("door event")

    @callback
    def request_wake(self, reason: str = "manual") -> None:
        self._wake(reason)

    # -- internals ------------------------------------------------------------
    @callback
    def _relock_check(self, _now) -> None:
        self._relock_unsub = None
        self._wake("post-unlock relock check")

    @callback
    def _check_idle(self, _now) -> None:
        if time.monotonic() - self._last_inbound >= self._heartbeat_s:
            self._wake("idle heartbeat")

    def _wake(self, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_wake < MIN_WAKE_GAP:
            _LOGGER.debug("wake suppressed (rate limit): %s", reason)
            return
        self._last_wake = now
        _LOGGER.debug("wake: %s", reason)
        self.hass.async_add_executor_job(self._listener.wake)
