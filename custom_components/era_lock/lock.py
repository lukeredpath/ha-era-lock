"""Lock entity (Layer 3) for the ERA Protect Lock.

A thin projection of the controller's state model. The hardware reports no
authoritative lock state and relocks automatically, so state is inferred by the
controller (see controller.py); this entity only renders ``signal_state`` updates
and delegates lock/unlock commands to the controller.

`assumed_state` is set, and the entity stays available without the local LAN
poll (unlock rides cloud MQTT).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EraConfigEntry
from .controller import LOCKED, EraController
from .coordinator import EraLockCoordinator
from .entity import device_info
from .events import signal_state

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraLockEntity(entry.runtime_data, entry)])


class EraLockEntity(CoordinatorEntity[EraLockCoordinator], LockEntity):
    """The door lock, rendering the controller's best-effort inferred state."""

    _attr_has_entity_name = True
    _attr_name = None  # main entity -> uses the device name
    _attr_assumed_state = True

    def __init__(self, coordinator: EraLockCoordinator, entry: EraConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.unique_id}_lock"
        self._attr_device_info = device_info(entry)
        self._state: str | None = None
        self._changed_by: str | None = None
        self._inferred: bool = True
        self._last_method: str | None = None
        self._last_user: str | None = None
        self._unlocking: bool = False  # transient "Unlocking…" UI state (local)

    @property
    def _controller(self) -> EraController:
        return self.coordinator.controller

    # -- lifecycle ------------------------------------------------------------
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_state(self._entry_id), self._on_state)
        )

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
    def is_unlocking(self) -> bool:
        return self._unlocking

    @property
    def changed_by(self) -> str | None:
        """What last changed the lock (shown in the UI + logbook)."""
        return self._changed_by

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "reason": self._changed_by,
            "inferred": self._inferred,
            "last_unlock_method": self._last_method,
            "last_unlock_by": self._last_user,
        }

    # -- state projection -----------------------------------------------------
    @callback
    def _on_state(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        self._state = payload.get("state")
        self._changed_by = payload.get("changed_by")
        self._inferred = payload.get("inferred", True)
        self._last_method = payload.get("last_method")
        self._last_user = payload.get("last_user")
        cause_ctx_id = payload.get("cause_ctx_id")
        user_id = payload.get("user_id")
        # Attribute the state change to its cause event (parent) + HA user, so the
        # activity log shows "triggered by <event>" / "By <user>". When neither is
        # present (a repeat of the same logged state) keep the existing context.
        if cause_ctx_id or user_id:
            self.async_set_context(Context(user_id=user_id, parent_id=cause_ctx_id))
        self.async_write_ha_state()

    # -- commands -------------------------------------------------------------
    async def _caller(self) -> tuple[str | None, str | None]:
        """The HA user behind the current service call (HA sets it as the entity
        context before the command runs), resolved to (user_id, name)."""
        uid = self._context.user_id if self._context else None
        if not uid:
            return None, None
        user = await self.hass.auth.async_get_user(uid)
        return uid, (user.name if user else None)

    async def async_lock(self, **kwargs: Any) -> None:
        uid, _ = await self._caller()
        await self._controller.async_lock(uid)

    async def async_unlock(self, **kwargs: Any) -> None:
        # Show the transitioning ("Unlocking…") state immediately; the cloud
        # round-trip takes a second or two. The controller performs the actual
        # transition, which comes back via signal_state.
        uid, name = await self._caller()
        self._unlocking = True
        self.async_write_ha_state()
        try:
            await self._controller.async_unlock(uid, name)
        finally:
            self._unlocking = False
            self.async_write_ha_state()
