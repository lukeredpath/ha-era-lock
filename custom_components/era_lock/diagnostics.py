"""Diagnostics for the ERA Protect Lock.

Runtime state of the controller / listener / wake manager plus the (redacted)
config entry. Every secret or identifying value is redacted: signing and device
keys, the MQTT client id, the Cognito refresh token, the whole Tuya session,
the hub / lock ids and IP, the account email, and any token/password.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EraConfigEntry
from .const import CONF_SESSION

# Keys redacted wherever they appear (async_redact_data recurses into dicts and
# lists). CONF_SESSION redacts the whole Tuya session dict.
TO_REDACT = {
    # secrets / key material
    "hmac_key", "local_key", "localKey", "chKey", "ch_key", "clientId",
    "mqtt_client_id", "clientid", "client_id",
    # Cognito / Tuya credentials
    "refresh_token", "id_token", "access_token", "token", "sid", "ecode",
    "password", "passwd", "tpassword", "username", "secret",
    CONF_SESSION, "uid", "tuid", "partner",
    # identifiers
    "hub_id", "lock_cid", "lock_id", "cid", "devId", "device_id", "hub_ip",
    "email", "unique_id", "user_id", "user_name", "cognito_client_id",
    "appsync_url",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EraConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = getattr(entry, "runtime_data", None)
    out: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "state": str(getattr(entry, "state", "")),
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
    }
    try:
        from homeassistant.loader import async_get_integration
        integration = await async_get_integration(hass, entry.domain)
        out["integration_version"] = str(integration.version)
    except Exception:  # noqa: BLE001 — diagnostics must not fail
        out["integration_version"] = None
    if coordinator is not None:
        out["runtime"] = {
            "started": coordinator.started,
            "closing": coordinator.closing,
            "auth_error": coordinator.auth_error,
            "has_local": coordinator.has_local,
            "lan_poll": {
                "last_update_success": coordinator.last_update_success,
                "update_interval_s": (coordinator.update_interval.total_seconds()
                                      if coordinator.update_interval else None),
                "dps": dict((coordinator.data or {}).get("dps") or {}),
            },
            "battery": {"value": coordinator.battery,
                        "source": coordinator.battery_source,
                        "updated": coordinator.battery_updated},
        }
        if coordinator.listener is not None:
            out["runtime"]["listener"] = coordinator.listener.diagnostics()
        wake = coordinator.wake
        if wake is not None:
            out["runtime"]["wake"] = {
                "heartbeat_s": wake.heartbeat_s,
                "close_wakes_pending": wake.close_wakes_pending,
                **wake.stats,
            }
        if coordinator.controller is not None:
            out["runtime"]["controller"] = coordinator.controller.diagnostics()
    return async_redact_data(out, TO_REDACT)
