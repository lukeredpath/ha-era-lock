"""Activity event entity (Layer 3) for the ERA Protect Lock.

A thin projection of the controller's discrete activity events (``signal_event``):
each enriched event is fired as an HA event entity event so automations and the
activity log can react to the specific thing that happened (an unlock by a named
credential, a device lock, a failed fingerprint, a hijack/tamper, a door change)
rather than only the derived lock state.
"""
from __future__ import annotations

import logging
from collections import deque

from homeassistant.components.event import EventEntity
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EraConfigEntry
from .entity import device_info
from .events import signal_event

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = ["unlock", "lock", "failed_unlock", "tamper", "door"]
_DEDUPE_MAX = 128


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraLockEventEntity(entry)])


class EraLockEventEntity(EventEntity):
    """Fires discrete lock activity events for automations + the activity log."""

    _attr_has_entity_name = True
    _attr_name = "Activity"
    _attr_translation_key = "activity"
    _attr_event_types = EVENT_TYPES

    def __init__(self, entry: EraConfigEntry) -> None:
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.unique_id}_event"
        self._attr_device_info = device_info(entry)
        # Bounded de-dupe so a buffered re-flush (same device-time) doesn't
        # double-fire the same discrete event.
        self._seen: set = set()
        self._seen_order: deque = deque(maxlen=_DEDUPE_MAX)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_event(self._entry_id), self._on_event)
        )

    @callback
    def _on_event(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        etype = payload.get("type")
        if etype not in EVENT_TYPES:
            return

        key = (etype, payload.get("dt"), payload.get("method"), payload.get("cred"))
        if key in self._seen:
            return
        if len(self._seen_order) == _DEDUPE_MAX:
            self._seen.discard(self._seen_order[0])  # about to be evicted
        self._seen.add(key)
        self._seen_order.append(key)

        cause_ctx_id = payload.get("cause_ctx_id")
        user_id = payload.get("user_id")
        # Make this event the activity-log root cause row (its id is the cause
        # context the lock state change parents to).
        if cause_ctx_id or user_id:
            self.async_set_context(Context(id=cause_ctx_id, user_id=user_id))

        dt = payload.get("dt")
        attributes = {
            "method": payload.get("method"),
            "credential_id": payload.get("cred"),
            "user": payload.get("user"),
            "user_id": user_id,
            "label": payload.get("label"),
            "actor": payload.get("actor"),
            "source": payload.get("source"),
            "subtype": payload.get("subtype"),
            "device_time": dt_util.utc_from_timestamp(dt).isoformat() if dt else None,
        }
        self._trigger_event(etype, attributes)
        self.async_write_ha_state()
