"""Buttons for the ERA Protect Lock.

- **Sync** publishes the synch DP (dp54) over cloud MQTT to force the BLE lock to
  reconnect and report its current DPs, without waiting for its next wake.
  Battery-conscious: it's a manual/automatable button, not a timer.
- **Mark as locked** asserts the door is locked (e.g. after a mechanical
  lock-up the lock didn't report). It actuates nothing — the lock relocks
  itself and has no remote lock command (``lock.lock`` raises) — it only sets
  the inferred state LOCKED, attributed to the HA user who pressed it.
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
    async_add_entities([
        EraSyncButton(entry.runtime_data, entry),
        EraMarkLockedButton(entry.runtime_data, entry),
    ])


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
        # Through the wake manager: coalesced with a wake sent moments ago, and
        # counted for the idle heartbeat.
        wake = self._coordinator.wake
        if wake is not None:
            wake.manual_sync()


class EraMarkLockedButton(ButtonEntity):
    """Manually assert the lock is locked (inferred; no actuation)."""

    _attr_has_entity_name = True
    _attr_translation_key = "mark_locked"
    _attr_icon = "mdi:lock-check"

    def __init__(self, coordinator: EraLockCoordinator, entry: EraConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.unique_id}_mark_locked"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        # HA sets the service call's context on the entity before pressing, so
        # its user is the person who pressed the button.
        user_id = self._context.user_id if self._context else None
        controller = self._coordinator.controller
        if controller is not None:
            controller.mark_locked(user_id)
