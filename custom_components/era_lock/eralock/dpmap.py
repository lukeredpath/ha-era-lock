"""Authoritative datapoint (DP) map + per-user attribution for the ERA lock.

The DP code map is the Tuya product schema for category `jtmspro` (product
x24twmcf), captured from the app (research/FINDINGS.md "AUTHORITATIVE DP MAP").
It is reference data, identical for every lock of this model, so it lives here as
a constant. The member ROSTER (credential-id -> person) IS per-device and is
loaded from configuration (lock_secrets.json "roster"), never hardcoded.
"""
from __future__ import annotations

# dpId -> schema code (authoritative, from the Tuya functionSchemaList).
DP_CODES: dict[int, str] = {
    1: "unlock_method_create",
    2: "unlock_method_delete",
    3: "unlock_method_modify",
    8: "residual_electricity",   # battery %
    11: "anti_lock_outside",     # deadbolt/anti-lock STATE (not an unlock command)
    12: "unlock_fingerprint",
    15: "unlock_card",           # NFC
    19: "unlock_ble",
    21: "alarm_lock",
    22: "hijack",
    25: "muffling",
    31: "beep_volume",
    32: "master_state",
    44: "rtc_lock",
    45: "alarm_active",
    54: "synch_method",          # enrolled-credential sync blob
    61: "remote_no_dp_key",      # 'AEkA' on remote unlock
    62: "unlock_phone_remote",
    63: "unlock_voice_remote",
    69: "record",
    70: "check_code_set",
    71: "ble_unlock_check",
    72: "unlock_record_check",
}

# Method DPs that carry a credential id on unlock -> humanised via the roster.
METHOD_DP_LABELS: dict[int, str] = {
    12: "fingerprint",
    15: "card",
    19: "ble",
    62: "phone_remote",
    63: "voice",
}

BATTERY_DP = 8
UNLOCK_EVENT_DP = 11  # pulses on any unlock; momentary, device-reported

# Example roster for reference/tests ONLY. Real names come from config
# (lock_secrets.json "roster": [{"dp":12,"value":1,"name":"Luke"}, ...]).
EXAMPLE_ROSTER = {
    (12, 1): "Luke",
    (12, 2): "Julie",
    (15, 0x33): "Main Key",
    (15, 0x34): "Luke",
}


def code(dp: int | str) -> str:
    """Schema code for a dp id, or 'dp<N>' if unknown."""
    try:
        return DP_CODES.get(int(dp), f"dp{dp}")
    except (TypeError, ValueError):
        return f"dp{dp}"


def roster_from_config(lock_secrets: dict) -> dict[tuple[int, int], str]:
    """Build a {(dp, value): name} roster from lock_secrets.json config.

    Accepts entries as {"dp":12,"value":1,"name":"Luke"}. Returns {} if none,
    so attribution degrades to the raw method+id rather than failing.
    """
    out: dict[tuple[int, int], str] = {}
    for e in lock_secrets.get("roster", []) or []:
        try:
            out[(int(e["dp"]), int(e["value"]))] = str(e["name"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def attribute(dp: int, value, roster: dict[tuple[int, int], str]) -> str:
    """Describe who/what an unlock DP represents, e.g. 'Luke (fingerprint)'.

    Falls back to '<method> #<value>' when the credential id isn't in the roster.
    """
    method = METHOD_DP_LABELS.get(int(dp), code(dp))
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return method
    name = roster.get((int(dp), ivalue))
    return f"{name} ({method})" if name else f"{method} #{ivalue}"
