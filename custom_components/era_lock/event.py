"""Activity event entity (Layer 3) for the ERA Protect Lock.

A thin projection of the controller's discrete activity events (``signal_event``):
each enriched event is fired as an HA event entity event so automations and the
activity log can react to the specific thing that happened (an unlock by a named
credential, a device lock, a failed fingerprint, a hijack/tamper, a door change)
rather than only the derived lock state.

Each (de-duplicated) activity is ALSO fired as an ``EVENT_ACTIVITY`` bus event,
described in logbook.py as the activity log's rich line ("Unlocked by Luke
(fingerprint)"). HA's logbook renders an event entity's own row only as its
translated ``event_type`` ("Unlocked"), so the rich line has to be a described
bus event. It's attached to the LOCK entity (and device) so the lock's own
activity shows who/how alongside its state rows, and it shares the activity's
cause context (so it's attributed "By <user>" and the lock / event entity state
changes link to it).
"""
from __future__ import annotations

import logging
from collections import deque

from homeassistant.components.event import EventEntity
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EraConfigEntry
from .const import DOMAIN, EVENT_ACTIVITY
from .entity import device_info
from .events import signal_event

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = ["unlock", "lock", "failed_unlock", "tamper", "door", "connectivity",
               "state_unconfirmed"]
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
        self._lock_unique_id = f"{entry.unique_id}_lock"  # see lock.py
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

        key = (etype, payload.get("dt"), payload.get("method"), payload.get("cred"),
               payload.get("subtype"))
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
        message = payload.get("message")
        self._fire_activity(etype, payload, message, cause_ctx_id, user_id)
        attributes = {
            "description": message,
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

    @callback
    def _fire_activity(
        self, etype: str, payload: dict, message: str | None,
        cause_ctx_id: str | None, user_id: str | None,
    ) -> None:
        """Fire the rich activity-log line for this activity. Fired BEFORE this
        entity's state write so the logbook memoises it as the context's root
        row (the event entity + lock state rows then show it as their cause)."""
        lock_entity_id = er.async_get(self.hass).async_get_entity_id(
            "lock", DOMAIN, self._lock_unique_id)
        device_id = self.registry_entry.device_id if self.registry_entry else None
        data = {
            # entity_id/device_id put the row in the lock's + device's activity
            # (the logbook matches $.entity_id / $.device_id in event data).
            "entity_id": lock_entity_id or self.entity_id,
            "device_id": device_id,
            "message": message or etype,
            "type": etype,
            "method": payload.get("method"),
            "credential_id": payload.get("cred"),
            "user": payload.get("user"),
            "label": payload.get("label"),
            "source": payload.get("source"),
            "subtype": payload.get("subtype"),
        }
        self.hass.bus.async_fire(
            EVENT_ACTIVITY, data,
            context=Context(id=cause_ctx_id, user_id=user_id))
