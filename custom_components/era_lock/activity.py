"""Human-readable activity text for the ERA Protect Lock (HA-free, pure).

One rich line per thing that happened — "Unlocked by Luke (fingerprint)",
"Auto-locked (timer)", "Failed fingerprint attempt" — used for:

  - the ``EVENT_ACTIVITY`` logbook row (described in logbook.py), which is the
    activity log's single rich line for each discrete event;
  - the event entity's ``description`` attribute;
  - the lock entity's ``changed_by`` / ``reason`` for a live unlock.

Deliberately free of Home Assistant imports so it is unit-testable offline.
"""
from __future__ import annotations

from .attribution import as_int

# Physical credentials whose numeric id is meaningful to show ("fingerprint #2").
# For the remote/app paths the "id" is a constant (dp62=0), so it's omitted.
_ID_METHODS = ("fingerprint", "card", "ble")

_METHOD_NAMES = {
    "ble": "Bluetooth",
    "phone_remote": "phone remote",
}

_ICONS = {
    "fingerprint": "mdi:fingerprint",
    "card": "mdi:card-account-details",
    "ble": "mdi:bluetooth",
    "phone_remote": "mdi:cellphone-key",
    "remote": "mdi:cellphone-key",
    "voice": "mdi:microphone",
    "thumbturn": "mdi:hand-back-right",
}


def method_name(method: str | None) -> str | None:
    """Display name for an unlock method (``phone_remote`` -> ``phone remote``)."""
    if not method:
        return None
    return _METHOD_NAMES.get(method, method.replace("_", " "))


def unlock_actor(
    method: str | None, cred, *, label: str | None = None,
    user_name: str | None = None, user_mapped: bool = False,
    who: str | None = None,
) -> str | None:
    """The named actor behind an unlock, or None when it's anonymous.

    Priority: mapped HA user > credential label > roster name (``who``)."""
    if user_mapped:
        return user_name or "a Home Assistant user"
    return label or who or None


def unlock_message(
    method: str | None, cred, *, source: str | None = None,
    label: str | None = None, user_name: str | None = None,
    user_mapped: bool = False, who: str | None = None,
) -> str:
    """``Unlocked by Luke (fingerprint)`` / ``Unlocked (fingerprint #2)`` /
    ``Unlocked (remote)`` / ``Unlocked in Home Assistant`` / ..."""
    if source == "command":
        return "Unlocked in Home Assistant"
    m = method_name(method)
    name = unlock_actor(method, cred, label=label, user_name=user_name,
                        user_mapped=user_mapped, who=who)
    if name:
        return f"Unlocked by {name} ({m})" if m else f"Unlocked by {name}"
    cred_i = as_int(cred)
    if method in _ID_METHODS and cred_i is not None:
        return f"Unlocked ({m} #{cred_i})"
    if m:
        return f"Unlocked ({m})"
    return "Unlocked"


def lock_message(source: str | None) -> str:
    if source == "timer":
        return "Auto-locked (timer)"
    if source == "command":
        return "Locked in Home Assistant"
    return "Device locked"


# Lock link state (see controller._set_online). While the hub reports the lock
# offline, actions at the door are neither reported live nor buffered, so the
# shown state can't be trusted until it reconnects.
LOCK_OFFLINE = "Lock offline (state unknown)"
LOCK_ONLINE = "Lock back online"
ONLINE_UNCONFIRMED = "Lock back online (state unconfirmed)"


def describe(
    etype: str, *, method: str | None = None, cred=None,
    source: str | None = None, subtype: str | None = None,
    label: str | None = None, user_name: str | None = None,
    user_mapped: bool = False, who: str | None = None,
) -> str:
    """The rich activity line for a discrete event of type ``etype``."""
    if etype == "unlock":
        return unlock_message(method, cred, source=source, label=label,
                              user_name=user_name, user_mapped=user_mapped,
                              who=who)
    if etype == "lock":
        return lock_message(source)
    if etype == "failed_unlock":
        m = method_name(method)
        return f"Failed {m} attempt" if m else "Failed unlock attempt"
    if etype == "tamper":
        return f"Tamper: {subtype.replace('_', ' ')}" if subtype else "Tamper"
    if etype == "door":
        if subtype == "open":
            return "Door opened"
        if subtype == "close":
            return "Door closed"
        return "Door"
    if etype == "connectivity":
        return LOCK_OFFLINE if subtype == "offline" else LOCK_ONLINE
    return etype.replace("_", " ").capitalize()


def icon(etype: str | None, method: str | None = None,
         subtype: str | None = None) -> str | None:
    """An mdi icon for the activity row."""
    if etype == "unlock":
        return _ICONS.get(method or "", "mdi:lock-open-variant")
    if etype == "lock":
        return "mdi:lock"
    if etype == "failed_unlock":
        return "mdi:fingerprint-off" if method == "fingerprint" else "mdi:lock-alert"
    if etype == "tamper":
        return "mdi:alert"
    if etype == "door":
        return "mdi:door-open" if subtype == "open" else "mdi:door-closed"
    if etype == "connectivity":
        return "mdi:lan-disconnect" if subtype == "offline" else "mdi:lan-connect"
    return None
