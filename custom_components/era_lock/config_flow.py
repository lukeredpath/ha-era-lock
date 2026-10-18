"""Config flow for ERA Protect Lock.

Two steps:
  1. user   — ERA email + password.
  2. config — paste `service_config.json` (the public, fleet-wide ERA/Tuya app
              constants) and enter the secret + sensitive values as individual
              fields (signing key, hub/lock keys and ids). Code-level constants
              (device_id, country_code, Tuya version) come from
              eralock.secrets.DEFAULTS. Everything is merged
              ({**DEFAULTS, **service_config, **secrets}) and each consumer picks
              the keys it needs.

Validation runs the real login (Cognito refresh token + Tuya session) and a
short Tuya-MQTT connect, so a created entry is known-good. A reauth step
re-mints credentials from the password when the refresh token lapses.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AUTO_RELOCK_SECONDS,
    CONF_CREDENTIALS,
    CONF_DOOR_CONTACT_ENTITY_ID,
    CONF_ERA,
    CONF_SECRETS,
    CONF_SERVICE_CONFIG,
    CONF_SESSION,
    CONF_WAKE_HEARTBEAT,
    CREDENTIAL_METHODS,
    DEFAULT_AUTO_RELOCK_SECONDS,
    DOMAIN,
)
from .eralock.era import EraAuthError, EraConnectionError, EraError
from .eralock.local import LocalLock
from .eralock.mqtt import CloudMqttLock, MqttUnlockError
from .eralock.secrets import DEFAULTS, SecretsError
from .session import EraSessionManager, initial_login

_LOGGER = logging.getLogger(__name__)

# service_config.json (pasted) must contain these public ERA/Tuya app constants.
_SERVICE_REQUIRED = ("cognito_client_id", "cognito_region", "appsync_url",
                     "clientId")
# Secret/sensitive values entered as individual fields.
SECRET_FIELDS = ("hmac_key", "local_key")                         # masked
SENSITIVE_FIELDS = ("hub_id", "lock_cid", "chKey", "mqtt_client_id")  # required
OPTIONAL_FIELDS = ("hub_ip", "lock_id")

_SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_TEXT = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
_JSON = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True))

_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})
_CONFIG_SCHEMA = vol.Schema({
    vol.Required(CONF_SERVICE_CONFIG): _JSON,
    **{vol.Required(k): _SECRET for k in SECRET_FIELDS},
    **{vol.Required(k): _TEXT for k in SENSITIVE_FIELDS},
    **{vol.Optional(k): _TEXT for k in OPTIONAL_FIELDS},
})


class _ValidationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _parse_service(blob: str) -> dict:
    try:
        d = json.loads(blob)
    except (json.JSONDecodeError, TypeError) as e:
        raise _ValidationError("invalid_json") from e
    if not isinstance(d, dict) or any(not d.get(k) for k in _SERVICE_REQUIRED):
        raise _ValidationError("invalid_service")
    return d


def _secrets_from_input(user_input: dict[str, Any]) -> dict:
    secrets = {k: user_input[k] for k in (*SECRET_FIELDS, *SENSITIVE_FIELDS)}
    for k in OPTIONAL_FIELDS:
        if user_input.get(k):
            secrets[k] = user_input[k]
    return secrets


def _validate_sync(email: str, password: str, combined: dict) -> dict:
    """Blocking: real login + MQTT connect. Returns the credential state."""
    refresh_token, session = initial_login(email, password, sign_secrets=combined)
    mgr = EraSessionManager(sign_secrets=combined, email=email,
                            refresh_token=refresh_token, session=session)
    # short connect-only check (subscribes, no publish -> never actuates)
    CloudMqttLock(combined, mgr.mqtt_secrets()).watch(0.3, lambda *_: None)
    # If a hub_ip is given, verify local_key with a real local read (the MQTT
    # connect above only validates the session creds, not local_key).
    if combined.get("hub_ip"):
        LocalLock(combined).dps()  # raises SecretsError (e.g. 914) on a bad key
    return {"refresh_token": refresh_token, "session": session}


async def _validate(hass: HomeAssistant, email: str, password: str,
                    combined: dict) -> dict:
    try:
        return await hass.async_add_executor_job(
            _validate_sync, email, password, combined)
    except EraAuthError as e:
        _LOGGER.debug("ERA auth failed: %s", e)
        raise _ValidationError("invalid_auth") from e
    except (EraConnectionError, requests.RequestException) as e:
        # Transport (DNS / timeout / 5xx / throttling / non-JSON): not the
        # user's credentials — let them retry rather than re-enter them.
        _LOGGER.debug("ERA service unreachable: %s", e)
        raise _ValidationError("cannot_connect") from e
    except EraError as e:
        _LOGGER.warning("ERA service rejected the request: %s", e)
        raise _ValidationError("unknown") from e
    except MqttUnlockError as e:
        _LOGGER.debug("Tuya MQTT connect failed: %s", e)
        raise _ValidationError("mqtt_failed") from e
    except SecretsError as e:
        _LOGGER.debug("Local read failed: %s", e)
        raise _ValidationError("bad_local_key") from e
    except _ValidationError:
        raise
    except Exception as e:  # noqa: BLE001
        _LOGGER.exception("Unexpected error validating ERA lock config")
        raise _ValidationError("unknown") from e


class EraLockConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ERA Protect Lock config + reauth flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "EraLockOptionsFlow":
        return EraLockOptionsFlow()

    def __init__(self) -> None:
        self._user: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_USER_SCHEMA)
        self._user = user_input
        return await self.async_step_config()

    async def async_step_config(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                service = _parse_service(user_input[CONF_SERVICE_CONFIG])
                secrets = _secrets_from_input(user_input)
                combined = {**DEFAULTS, **service, **secrets}
                email = self._user[CONF_USERNAME]
                creds = await _validate(self.hass, email,
                                        self._user[CONF_PASSWORD], combined)
            except _ValidationError as e:
                errors["base"] = e.code
            else:
                await self.async_set_unique_id(str(secrets["hub_id"]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="ERA Protect Lock",
                    data={
                        CONF_ERA: {"refresh_token": creds["refresh_token"], "email": email},
                        CONF_SESSION: creds["session"].to_dict(),
                        CONF_SERVICE_CONFIG: service,
                        CONF_SECRETS: secrets,
                    },
                )
        return self.async_show_form(
            step_id="config", data_schema=_CONFIG_SCHEMA, errors=errors)

    # ------------------------------------------------------------------ reauth
    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            email = entry.data[CONF_ERA]["email"]
            combined = {**DEFAULTS, **entry.data[CONF_SERVICE_CONFIG],
                        **entry.data[CONF_SECRETS]}
            try:
                creds = await _validate(self.hass, email,
                                        user_input[CONF_PASSWORD], combined)
            except _ValidationError as e:
                errors["base"] = e.code
            else:
                data = {
                    **entry.data,
                    CONF_ERA: {"refresh_token": creds["refresh_token"], "email": email},
                    CONF_SESSION: creds["session"].to_dict(),
                }
                if entry.update_listeners:
                    # Loaded (the session died at runtime): the entry's update
                    # listener sees credentials it didn't persist itself and
                    # reloads (HA wants listener-owned reloads, not both).
                    return self.async_update_and_abort(entry, data=data)
                # Setup failed (ConfigEntryAuthFailed): no listener — reload here.
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"email": entry.data[CONF_ERA]["email"]},
        )


_ADD_CREDENTIAL = "__add__"
_NO_USER = "__none__"


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _credential_summary(c: dict) -> str:
    base = f"{c.get('method', '?')} #{c.get('id', '?')}"
    who = c.get("user_name") or (c.get("label") or "").strip()
    return f"{base} — {who}" if who else base


class EraLockOptionsFlow(OptionsFlow):
    """Tuning options (relock window, door contact, heartbeat) plus per-credential
    labels and optional HA-user mapping for fingerprint/card attribution."""

    def __init__(self) -> None:
        self._edit_index: int | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "credentials"])

    def _save(self, update: dict[str, Any]) -> ConfigFlowResult:
        """Merge an update into the existing options (never replace the lot)."""
        data = dict(self.config_entry.options)
        data.update(update)
        return self.async_create_entry(title="", data=data)

    # -- lock settings --------------------------------------------------------
    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            cleaned = {k: v for k, v in user_input.items() if v not in (None, "")}
            data = dict(self.config_entry.options)
            for k in (CONF_AUTO_RELOCK_SECONDS, CONF_DOOR_CONTACT_ENTITY_ID,
                      CONF_WAKE_HEARTBEAT):
                if k in cleaned:
                    data[k] = cleaned[k]
                else:
                    data.pop(k, None)  # optional field cleared
            return self.async_create_entry(title="", data=data)

        o = self.config_entry.options
        schema = vol.Schema({
            vol.Required(
                CONF_AUTO_RELOCK_SECONDS,
                default=o.get(CONF_AUTO_RELOCK_SECONDS, DEFAULT_AUTO_RELOCK_SECONDS),
            ): NumberSelector(NumberSelectorConfig(
                min=60, max=600, step=1, mode=NumberSelectorMode.BOX,
                unit_of_measurement="s")),
            vol.Optional(
                CONF_DOOR_CONTACT_ENTITY_ID,
                description={"suggested_value": o.get(CONF_DOOR_CONTACT_ENTITY_ID)},
            ): EntitySelector(EntitySelectorConfig(
                domain="binary_sensor", device_class="door")),
            # Optional: left empty, the default follows the door contact (see
            # const.default_wake_heartbeat), so adding/removing a door sensor
            # retunes it — a stored value is only ever an explicit choice.
            vol.Optional(
                CONF_WAKE_HEARTBEAT,
                description={"suggested_value": o.get(CONF_WAKE_HEARTBEAT)},
            ): NumberSelector(NumberSelectorConfig(
                min=0, max=3600, step=60, mode=NumberSelectorMode.BOX,
                unit_of_measurement="s")),
        })
        return self.async_show_form(step_id="settings", data_schema=schema)

    # -- credentials: pick one to edit, or add ---------------------------------
    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        creds = list(self.config_entry.options.get(CONF_CREDENTIALS, []))
        if user_input is not None:
            sel = user_input["credential"]
            self._edit_index = None if sel == _ADD_CREDENTIAL else int(sel)
            return await self.async_step_credential()
        choices = {_ADD_CREDENTIAL: "➕ Add a credential"}
        for i, c in enumerate(creds):
            choices[str(i)] = _credential_summary(c)
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema({vol.Required("credential"): vol.In(choices)}))

    # -- credentials: add / edit / delete one ----------------------------------
    async def async_step_credential(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        creds = list(self.config_entry.options.get(CONF_CREDENTIALS, []))
        existing = creds[self._edit_index] if self._edit_index is not None else {}
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get("delete") and self._edit_index is not None:
                creds.pop(self._edit_index)
                return self._save({CONF_CREDENTIALS: creds})
            cred_id = _as_int(user_input.get("id"))
            if cred_id is None:
                errors["base"] = "invalid_id"
            else:
                entry: dict[str, Any] = {
                    "method": user_input["method"],
                    "id": cred_id,
                    "label": (user_input.get("label") or "").strip(),
                }
                uid = user_input.get("user_id")
                if uid and uid != _NO_USER:
                    entry["user_id"] = uid
                    u = await self.hass.auth.async_get_user(uid)
                    if u is not None:
                        entry["user_name"] = u.name
                if self._edit_index is not None:
                    creds[self._edit_index] = entry
                else:
                    creds.append(entry)
                return self._save({CONF_CREDENTIALS: creds})

        users = await self.hass.auth.async_get_users()
        user_choices = {_NO_USER: "— No HA user (use label) —"}
        for u in users:
            if u.is_active and not u.system_generated and u.name:
                user_choices[u.id] = u.name

        fields: dict[Any, Any] = {
            vol.Required(
                "method", default=existing.get("method", CREDENTIAL_METHODS[0]),
            ): vol.In(list(CREDENTIAL_METHODS)),
            vol.Required(
                "id", default=existing.get("id", 1),
            ): NumberSelector(NumberSelectorConfig(
                min=0, max=255, step=1, mode=NumberSelectorMode.BOX)),
            vol.Optional(
                "label", description={"suggested_value": existing.get("label")},
            ): _TEXT,
            vol.Optional(
                "user_id", default=existing.get("user_id", _NO_USER),
            ): vol.In(user_choices),
        }
        if self._edit_index is not None:
            fields[vol.Optional("delete", default=False)] = bool
        return self.async_show_form(
            step_id="credential", data_schema=vol.Schema(fields), errors=errors)
