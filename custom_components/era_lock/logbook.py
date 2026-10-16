"""Logbook describer for the ERA Protect Lock.

The lock fires an ``EVENT_CAUSE`` event (with a context) as the parent of each
state change; describing it here lets HA's activity log show the cause in the
state change's "what happened" box. The event carries no entity_id, so it is
used only as the state change's context source and doesn't render as its own
line.
"""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import CAUSE_SOURCE, DOMAIN, EVENT_CAUSE


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict]], None],
) -> None:
    @callback
    def describe(event: Event) -> dict:
        return {
            LOGBOOK_ENTRY_NAME: event.data.get("name", CAUSE_SOURCE),
            LOGBOOK_ENTRY_MESSAGE: event.data.get("message", ""),
        }

    async_describe_event(DOMAIN, EVENT_CAUSE, describe)
