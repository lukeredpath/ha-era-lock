"""Lock entity for the ERA Protect Lock.

Cloud-assisted unlock over Tuya MQTT (the proven path). The hardware reports no
authoritative lock state and relocks automatically, so state is inferred:

  - unlock (push event / command) -> `unlocked`, start the auto-relock timer;
  - timer elapses with the door still shut -> `locked`;
  - an optional door-contact sensor corrects the silent mechanical path: the
    door cannot open while deadbolt-locked, so a door **open** means `unlocked`
    (this catches a manual thumbturn), and after the door **closes** the state
    is `unknown` (a silent manual relock is unknowable) until the next event;
  - `lock()` is a manual "mark as locked" so the user can resolve `unknown`.

`assumed_state` is set, and the entity stays available without the local LAN
poll (unlock rides cloud MQTT).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.components.logbook import async_log_entry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EraConfigEntry
from .const import (
    CONF_AUTO_RELOCK_SECONDS,
    CONF_DOOR_CONTACT_ENTITY_ID,
    DEFAULT_AUTO_RELOCK_SECONDS,
    DOMAIN,
)
from .coordinator import EraLockCoordinator
from .entity import device_info
from .eralock.mqtt import CloudMqttLock, MqttUnlockError
from .mqtt_listener import signal_state

_LOGGER = logging.getLogger(__name__)

LOCKED = "locked"
UNLOCKED = "unlocked"


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraLockEntity(entry.runtime_data, entry)])


class EraLockEntity(CoordinatorEntity[EraLockCoordinator], LockEntity):
    """The door lock, with best-effort inferred state."""

    _attr_has_entity_name = True
    _attr_name = None  # main entity -> uses the device name
    _attr_assumed_state = True

    def __init__(self, coordinator: EraLockCoordinator, entry: EraConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.unique_id}_lock"
        self._attr_device_info = device_info(entry)
        self._state: str | None = None
        self._relock_cancel = None
        self._changed_by: str | None = None
        self._last_method: str | None = None
        self._last_user: str | None = None
        self._logged_state: str | None = "__init__"  # for logbook de-dupe

    # -- options --------------------------------------------------------------
    @property
    def _options(self) -> dict:
        return dict(self.coordinator.config_entry.options)

    @property
    def _relock_seconds(self) -> int:
        try:
            return int(self._options.get(CONF_AUTO_RELOCK_SECONDS, DEFAULT_AUTO_RELOCK_SECONDS))
        except (TypeError, ValueError):
            return DEFAULT_AUTO_RELOCK_SECONDS

    @property
    def _door_entity(self) -> str | None:
        return self._options.get(CONF_DOOR_CONTACT_ENTITY_ID)

    # -- lifecycle ------------------------------------------------------------
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # State starts unknown until an unlock event (dp11 is too ambiguous to
        # seed from); the fallback relock timer + mark-as-locked resolve it.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_state(self._entry_id), self._on_push_state)
        )
        if self._door_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._door_entity], self._on_door)
            )
        self.async_on_remove(self._cancel_relock)

    @property
    def available(self) -> bool:
        # Unlock rides cloud MQTT, independent of the local LAN poll.
        return True

    @property
    def is_locked(self) -> bool | None:
        if self._state is None:
            return None
        return self._state == LOCKED

    @property
    def changed_by(self) -> str | None:
        """What last changed the lock (shown in the UI + logbook)."""
        return self._changed_by

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "reason": self._changed_by,
            "last_unlock_method": self._last_method,
            "last_unlock_by": self._last_user,
        }

    @callback
    def _set(self, state: str | None) -> None:
        self._state = state
        self.async_write_ha_state()

    @callback
    def _transition(self, state: str | None, reason: str) -> None:
        """Change state, record what caused it, and log it to the activity log."""
        self._changed_by = reason
        _LOGGER.debug("lock -> %s  (%s)", state or "unknown", reason)
        if state != self._logged_state:
            self._logged_state = state
            verb = {LOCKED: "locked", UNLOCKED: "unlocked"}.get(state, "state unknown")
            async_log_entry(
                self.hass, "ERA Protect Lock", f"{verb} — {reason}",
                DOMAIN, self.entity_id)
        self._set(state)

    # -- auto-relock timer ----------------------------------------------------
    @callback
    def _start_relock(self) -> None:
        self._cancel_relock()
        if self._relock_seconds > 0:
            self._relock_cancel = async_call_later(
                self.hass, self._relock_seconds, self._relock_fired)

    @callback
    def _cancel_relock(self) -> None:
        if self._relock_cancel is not None:
            self._relock_cancel()
            self._relock_cancel = None

    @callback
    def _relock_fired(self, _now) -> None:
        self._relock_cancel = None
        if self._door_open():
            return  # door-open suppresses the timed relock
        self._transition(LOCKED, "auto-relock (timer)")

    def _door_open(self) -> bool:
        if not self._door_entity:
            return False
        st = self.hass.states.get(self._door_entity)
        return st is not None and st.state == "on"  # door device_class: on=open

    # -- inputs ---------------------------------------------------------------
    @callback
    def _on_push_state(self, payload) -> None:
        # payload is {"locked": bool, "method": str|None, "who": str|None}
        locked = payload.get("locked") if isinstance(payload, dict) else bool(payload)
        method = payload.get("method") if isinstance(payload, dict) else None
        who = payload.get("who") if isinstance(payload, dict) else None
        if locked:
            self._cancel_relock()
            self._transition(LOCKED, method or "event")
        else:
            self._last_method = method
            self._last_user = who
            self._transition(UNLOCKED, who or method or "unlock")
            self._start_relock()

    @callback
    def _on_door(self, event: Event) -> None:
        new = event.data.get("new_state")
        if new is None:
            return
        if new.state == "on":  # opened -> can't be deadbolt-locked
            self._cancel_relock()
            self._transition(UNLOCKED, "door opened")
        elif new.state == "off":  # closed -> silent relock is unknowable
            self._cancel_relock()
            self._transition(None, "door closed")

    # -- commands -------------------------------------------------------------
    async def async_lock(self, **kwargs: Any) -> None:
        # No hardware lock command (the lock auto-relocks), so this is a manual
        # "mark as locked" to assert the secured state the silent mechanical
        # thumbturn doesn't report.
        self._cancel_relock()
        self._transition(LOCKED, "Home Assistant (manual)")

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(self._unlock)
        self._last_method = "remote"
        self._last_user = "Home Assistant"
        self._transition(UNLOCKED, "Home Assistant (manual)")
        self._start_relock()
        await self.coordinator.async_request_refresh()

    def _unlock(self) -> None:
        mgr = self.coordinator.mgr
        combined = self.coordinator.combined
        try:
            ok = CloudMqttLock(combined, mgr.mqtt_secrets()).unlock()
        except MqttUnlockError:
            _LOGGER.debug("MQTT unlock failed; refreshing Tuya session and retrying")
            mgr.refresh_tuya_session()
            ok = CloudMqttLock(combined, mgr.mqtt_secrets()).unlock()
        if not ok:
            raise HomeAssistantError("Unlock command sent but not confirmed by the lock.")
