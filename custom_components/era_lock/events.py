"""Canonical event model + dispatcher signals for the ERA Protect Lock.

The three layers communicate over three HA dispatcher signals:
  - ``signal_raw``   Layer 1 (sources) -> Layer 2 (controller): normalised
    :class:`LockEvent`s, no decision logic attached;
  - ``signal_event`` Layer 2 -> the event entity: enriched discrete activity
    events (unlock/lock/failed_unlock/tamper/door) for the activity log;
  - ``signal_state`` Layer 2 -> the lock entity: state-model updates the lock
    entity renders verbatim.

A :class:`LockEvent` is the single currency Layer 1 produces. It is deliberately
plain data (no HA imports) so the normalisation/decision logic is testable
offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import DOMAIN


@dataclass(frozen=True)
class LockEvent:
    """A normalised input event from any source.

    ``type``    one of ``state_report`` | ``unlock`` | ``failed_unlock`` |
                ``tamper`` | ``door``.
    ``dt``      device-time epoch for the event; for door/command/timer events
                (which the device doesn't timestamp) the wall-clock ``time.time()``.
    ``source``  ``device`` | ``command`` | ``door`` | ``timer``.
    """

    type: str
    dt: float
    fresh: bool = True
    source: str = "device"
    method: str | None = None
    cred: int | None = None
    who: str | None = None
    user_id: str | None = None
    locked: bool | None = None
    subtype: str | None = None
    value: Any = None


def signal_raw(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_raw"


def signal_event(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_event"


def signal_state(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_state"
