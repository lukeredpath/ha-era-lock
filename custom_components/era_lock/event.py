"""Unlock event entity for the ERA Protect Lock.

Fires an `unlock` event (with method + attribution) whenever the cloud-MQTT
listener reports an unlock. Drives the HA Logbook.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EraConfigEntry
from .entity import device_info
from .mqtt_listener import signal_event


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraUnlockEvent(entry)])


class EraUnlockEvent(EventEntity):
    """Unlock events with per-credential attribution."""

    _attr_has_entity_name = True
    _attr_name = "Unlock"
    _attr_event_types = ["unlock"]
    _attr_icon = "mdi:lock-open-variant"

    def __init__(self, entry: EraConfigEntry) -> None:
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.unique_id}_unlock_event"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_event(self._entry_id), self._handle)
        )

    @callback
    def _handle(self, payload: dict[str, Any]) -> None:
        self._trigger_event("unlock", payload)
        self.async_write_ha_state()
