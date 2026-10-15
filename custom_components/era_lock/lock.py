"""Lock entity for the ERA Protect Lock.

Cloud-assisted unlock over Tuya MQTT (the proven path). The hardware reports no
authoritative lock state and relocks automatically, so:
  - `assumed_state` is set,
  - state is read best-effort from the electronic anti-lock datapoint (dp11)
    from the local poll (live push comes in a later milestone),
  - `lock()` is unsupported (the lock auto-relocks; there is no lock command).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EraConfigEntry
from .coordinator import EraLockCoordinator
from .entity import device_info
from .eralock.mqtt import CloudMqttLock, MqttUnlockError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraLockEntity(entry.runtime_data, entry)])


class EraLockEntity(CoordinatorEntity[EraLockCoordinator], LockEntity):
    """The door lock."""

    _attr_has_entity_name = True
    _attr_name = None  # main entity -> uses the device name
    _attr_assumed_state = True

    def __init__(self, coordinator: EraLockCoordinator, entry: EraConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_lock"
        self._attr_device_info = device_info(entry)
        self._optimistic: bool | None = None

    @property
    def available(self) -> bool:
        # Unlock rides cloud MQTT, which doesn't need the local LAN poll, so the
        # lock stays available even when the hub can't be reached locally; the
        # state just becomes unknown.
        return True

    @property
    def is_locked(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        v = (self.coordinator.data or {}).get("anti_lock")
        return None if v is None else bool(v)

    async def async_lock(self, **kwargs: Any) -> None:
        # No hardware lock command (the lock auto-relocks), so this is a manual
        # "mark as locked": optimistically assert the secured state the
        # integration can't otherwise detect (the mechanical thumbturn is silent).
        self._optimistic = True
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(self._unlock)
        self._optimistic = False  # unlocked
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    def _unlock(self) -> None:
        mgr = self.coordinator.mgr
        combined = self.coordinator.combined
        try:
            ok = CloudMqttLock(combined, mgr.mqtt_secrets()).unlock()
        except MqttUnlockError:
            # Session/MQTT creds likely expired -> re-mint and retry once.
            _LOGGER.debug("MQTT unlock failed; refreshing Tuya session and retrying")
            mgr.refresh_tuya_session()
            ok = CloudMqttLock(combined, mgr.mqtt_secrets()).unlock()
        if not ok:
            raise HomeAssistantError("Unlock command sent but not confirmed by the lock.")
