"""Sensors for the ERA Protect Lock (battery)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EraConfigEntry
from .coordinator import EraLockCoordinator
from .entity import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraBatterySensor(entry.runtime_data, entry)])


class EraBatterySensor(CoordinatorEntity[EraLockCoordinator], SensorEntity):
    """Battery % from the lock's local datapoint (dp8)."""

    _attr_has_entity_name = True
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EraLockCoordinator, entry: EraConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_battery"
        self._attr_device_info = device_info(entry)

    @property
    def native_value(self) -> int | None:
        return (self.coordinator.data or {}).get("battery")

    @property
    def available(self) -> bool:
        return super().available and (self.coordinator.data or {}).get("battery") is not None
