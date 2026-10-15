"""Centralised loading of key material as configuration values.

Every secret the lock needs (hub localKey, lock cid, Tuya app signing material,
member roster) is a configuration input, not a baked-in constant. Today they are
read from gitignored JSON files at the repo root; the Home Assistant integration
will supply the same dict shapes from its config entry instead. Keep this module
the ONLY place that knows where secrets come from.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Repo root = two levels up from this file (src/eralock/secrets.py). Overridable
# so HA / tests can point elsewhere without touching callers.
_DEFAULT_HOME = Path(__file__).resolve().parents[2]
HOME = Path(os.environ.get("ERA_LOCK_HOME", _DEFAULT_HOME))

LOCK_SECRETS_PATH = HOME / "lock_secrets.json"
SIGN_SECRETS_PATH = HOME / "sign_secrets.json"
MQTT_SECRETS_PATH = HOME / "mqtt_secrets.json"
STATE_PATH = HOME / "lock_state.json"  # local inference cache (gitignored)


class SecretsError(RuntimeError):
    """A required secrets file is missing, malformed, or incomplete."""


def redact(key: str | None) -> str:
    """Render a secret for logs without disclosing it."""
    if not key:
        return "<empty>"
    return f"{key[:3]}…{key[-2:]} ({len(key)} chars)"


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise SecretsError(
            f"{path.name} not found at {path.parent}. Populate it from the research "
            f"capture (see research/FINDINGS.md), or set ERA_LOCK_HOME."
        )
    try:
        with path.open() as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise SecretsError(f"{path.name} is not valid JSON: {e}") from e


def load_lock_secrets(path: Path | str | None = None) -> dict:
    """Local-control secrets (hub id/ip/localKey, lock cid, DP overrides, roster)."""
    d = _load_json(Path(path) if path else LOCK_SECRETS_PATH)
    for required in ("hub_id", "hub_ip", "local_key"):
        if not d.get(required):
            raise SecretsError(f"lock_secrets.json is missing required field: {required}")
    return d


def load_sign_secrets(path: Path | str | None = None) -> dict:
    """Tuya mobile-API signing material (appSecret, clientId, assembled hmac_key)."""
    d = _load_json(Path(path) if path else SIGN_SECRETS_PATH)
    if not (d.get("hmac_key") or (d.get("package") and d.get("appSecret"))):
        raise SecretsError("sign_secrets.json has no usable hmac_key / signing material")
    return d


def load_mqtt_secrets(path: Path | str | None = None) -> dict:
    """Tuya MQTT connect credentials (broker, clientId, username, password).

    NOTE: these are SESSION-derived and ephemeral — captured live from the app
    (research/frida/frida_mqtt_creds.js). Until the login-derivation (step B) is
    implemented, they expire and must be re-captured. In HA this is the slot the
    config entry / a login routine will fill.
    """
    d = _load_json(Path(path) if path else MQTT_SECRETS_PATH)
    for required in ("broker", "clientId", "username", "password"):
        if not d.get(required):
            raise SecretsError(f"mqtt_secrets.json is missing required field: {required}")
    return d
