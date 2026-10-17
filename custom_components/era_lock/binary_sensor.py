"""Connectivity binary sensor (Layer 3) for the ERA Protect Lock.

A thin projection of the controller's view of the lock's link (``signal_link``):
on = the hub reports the lock reachable (awake, or asleep/"nearby" — its normal
idle), off = the hub reports it offline. The controller decides (see
controller._set_online); this entity only renders.

Always available: it is the entity reporting the outage, so it must stay up
while the lock entity is unavailable. Starts from the controller's current view,
which assumes online at startup — the hub only reports presence on change, so
"unknown until told" would mean unknown indefinitely for a healthy lock; and it
keeps the sensor consistent with the lock entity (available at startup).

Each transition row parents to the controller's "Lock offline" / "Lock back
online" activity line.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EraConfigEntry
from .coordinator import EraLockCoordinator
from .entity import device_info
from .events import signal_link


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraLockConnectedSensor(entry.runtime_data, entry)])


class EraLockConnectedSensor(BinarySensorEntity):
    """Whether the hub reports the lock connected (awake or asleep/nearby)."""

    _attr_has_entity_name = True
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator: EraLockCoordinator, entry: EraConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.unique_id}_connected"
        self._attr_device_info = device_info(entry)
        controller = coordinator.controller
        self._attr_is_on = controller.online if controller is not None else True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_link(self._entry_id), self._on_link)
        )

    @property
    def available(self) -> bool:
        return True  # reports the outage; never goes unavailable with the lock

    @callback
    def _on_link(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        self._attr_is_on = bool(payload.get("online"))
        cause_ctx_id = payload.get("cause_ctx_id")
        if cause_ctx_id:
            self.async_set_context(Context(parent_id=cause_ctx_id))
        self.async_write_ha_state()
