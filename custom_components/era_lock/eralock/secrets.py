"""Centralised loading of key material as configuration values.

Config is split into two files at the repo root (override the location with the
ERA_LOCK_HOME env var):
  - service_config.json : non-secret ERA/Tuya service + device config
  - secrets.json        : secret key material (hmac_key, local keys, ...)

They are merged ({**service_config, **secrets}) into the single dict the rest of
the library consumes; each consumer picks the keys it needs. The Home Assistant
integration supplies the same merged dict from its config entry instead. Keep
this module the ONLY place that knows where secrets come from.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Repo root = two levels up from this file. Overridable so HA / tests can point
# elsewhere without touching callers.
_DEFAULT_HOME = Path(__file__).resolve().parents[2]
HOME = Path(os.environ.get("ERA_LOCK_HOME", _DEFAULT_HOME))

SERVICE_CONFIG_PATH = HOME / "service_config.json"
SECRETS_PATH = HOME / "secrets.json"
MQTT_SECRETS_PATH = HOME / "mqtt_secrets.json"
STATE_PATH = HOME / "lock_state.json"  # local inference cache (gitignored)

# Constants that belong in code, not config: an arbitrary client device id,
# regional defaults, and the Tuya LAN protocol version. Merged into every
# combined dict; a value present in the config files overrides them.
DEFAULTS: dict = {
    "device_id": "era-lock-cli",
    "country_code": "+44",
    "version": 3.4,
}


class SecretsError(RuntimeError):
    """A required config/secrets file is missing, malformed, or incomplete."""


def redact(key: str | None) -> str:
    """Render a secret for logs without disclosing it."""
    if not key:
        return "<empty>"
    return f"{key[:3]}…{key[-2:]} ({len(key)} chars)"


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise SecretsError(
            f"{path.name} not found at {path.parent}. Copy {path.stem}.template.json "
            f"to {path.name} and fill it in, or set ERA_LOCK_HOME."
        )
    try:
        with path.open() as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise SecretsError(f"{path.name} is not valid JSON: {e}") from e


def load_service_config(path: Path | str | None = None) -> dict:
    """Non-secret ERA/Tuya service + device config."""
    return _load_json(Path(path) if path else SERVICE_CONFIG_PATH)


def load_secrets(path: Path | str | None = None) -> dict:
    """Secret key material (hmac_key, local keys, ...)."""
    return _load_json(Path(path) if path else SECRETS_PATH)


def load_combined() -> dict:
    """Merge code DEFAULTS + service_config + secrets (later wins on overlap)."""
    return {**DEFAULTS, **load_service_config(), **load_secrets()}


def load_sign_secrets(path: Path | str | None = None) -> dict:
    """Signing / ERA-bridge material (merged). Requires hmac_key + clientId."""
    d = load_combined()
    if not d.get("hmac_key"):
        raise SecretsError("secrets.json is missing 'hmac_key'")
    if not d.get("clientId"):
        raise SecretsError("service_config.json is missing 'clientId'")
    return d


def load_lock_secrets(path: Path | str | None = None) -> dict:
    """Local/cloud device material (merged). Requires hub_id + local_key."""
    d = load_combined()
    for required in ("hub_id", "local_key"):
        if not d.get(required):
            raise SecretsError(f"config is missing required field: {required}")
    return d


def load_mqtt_secrets(path: Path | str | None = None) -> dict:
    """Tuya MQTT connect credentials (broker, clientId, username, password).

    NOTE: these are SESSION-derived and ephemeral. In HA this slot is filled by
    deriving them from the session at runtime (see eralock.session).
    """
    d = _load_json(Path(path) if path else MQTT_SECRETS_PATH)
    for required in ("broker", "clientId", "username", "password"):
        if not d.get(required):
            raise SecretsError(f"mqtt_secrets.json is missing required field: {required}")
    return d
