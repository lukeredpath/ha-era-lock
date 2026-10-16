"""Sync button for the ERA Protect Lock.

Publishes the synch DP (dp54) over cloud MQTT to force the BLE lock to reconnect
and report its current DPs, without waiting for its next wake. Battery-conscious:
it's a manual/automatable button, not a timer.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EraConfigEntry
from .coordinator import EraLockCoordinator
from .entity import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraSyncButton(entry.runtime_data, entry)])


class EraSyncButton(ButtonEntity):
    """Force the lock to reconnect and report its DPs (dp54 synch)."""

    _attr_has_entity_name = True
    _attr_name = "Sync"
    _attr_icon = "mdi:sync"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EraLockCoordinator, entry: EraConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.unique_id}_sync"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        listener = self._coordinator.listener
        if listener is not None:
            await self.hass.async_add_executor_job(listener.wake)
