"""Logbook describer for the ERA Protect Lock.

``EVENT_ACTIVITY`` is fired once per discrete activity (by the event entity,
after its de-dupe) and is the activity log's single rich line for it:
"Unlocked by Luke (fingerprint)", "Auto-locked (assumed)", "Failed fingerprint
attempt", ... Its ``entity_id`` is the lock entity, so the frontend renders it as
``<lock name>: <message>`` in the global, device and lock-entity activity views;
its context carries the activity's HA user (shown "By <user>") and is the parent
of the lock's state change, which the frontend therefore shows as caused by it.

``EVENT_CAUSE`` is the per-state-change cause event of earlier versions. It is no
longer fired but is still described so already-recorded history renders.
"""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from . import activity
from .const import CAUSE_SOURCE, DOMAIN, EVENT_ACTIVITY, EVENT_CAUSE


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict]], None],
) -> None:
    @callback
    def describe_activity(event: Event) -> dict:
        data = event.data
        out = {
            LOGBOOK_ENTRY_NAME: CAUSE_SOURCE,
            LOGBOOK_ENTRY_MESSAGE: data.get("message") or data.get("type", ""),
        }
        if entity_id := data.get("entity_id"):
            out[LOGBOOK_ENTRY_ENTITY_ID] = entity_id
        if icon := activity.icon(data.get("type"), data.get("method"),
                                 data.get("subtype")):
            out[LOGBOOK_ENTRY_ICON] = icon
        return out

    @callback
    def describe_legacy_cause(event: Event) -> dict:
        return {
            LOGBOOK_ENTRY_NAME: event.data.get("name", CAUSE_SOURCE),
            LOGBOOK_ENTRY_MESSAGE: event.data.get("message", ""),
        }

    async_describe_event(DOMAIN, EVENT_ACTIVITY, describe_activity)
    async_describe_event(DOMAIN, EVENT_CAUSE, describe_legacy_cause)
