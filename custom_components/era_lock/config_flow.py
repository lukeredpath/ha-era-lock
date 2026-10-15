"""Config flow for ERA Protect Lock.

Two steps:
  1. user    — ERA email + password.
  2. config  — paste the two config files:
                 * service_config — non-secret ERA/Tuya service + device config
                 * secrets        — secret key material (hmac_key, local keys)
               They are merged ({**service_config, **secrets}); each consumer
               picks the keys it needs. Nothing is bundled in the repo.

Validation runs the real login (Cognito refresh token + Tuya session) and a
short Tuya-MQTT connect, so a created entry is known-good. A reauth step
re-mints credentials from the password when the refresh token lapses.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ERA,
    CONF_SECRETS,
    CONF_SERVICE_CONFIG,
    CONF_SESSION,
    DOMAIN,
)
from .eralock.era import EraAuthError
from .eralock.mqtt import CloudMqttLock, MqttUnlockError
from .session import EraSessionManager, initial_login

_LOGGER = logging.getLogger(__name__)

_SERVICE_REQUIRED = ("cognito_client_id", "cognito_region", "appsync_url",
                     "clientId", "chKey", "mqtt_client_id", "hub_id", "lock_cid")
_SECRETS_REQUIRED = ("hmac_key", "local_key")

_JSON = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True))

_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})
_CONFIG_SCHEMA = vol.Schema({
    vol.Required(CONF_SERVICE_CONFIG): _JSON,
    vol.Required(CONF_SECRETS): _JSON,
})


class _ValidationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _parse(blob: str, required: tuple[str, ...], err: str) -> dict:
    try:
        d = json.loads(blob)
    except (json.JSONDecodeError, TypeError) as e:
        raise _ValidationError("invalid_json") from e
    if not isinstance(d, dict) or any(not d.get(k) for k in required):
        raise _ValidationError(err)
    return d


def _validate_sync(email: str, password: str, combined: dict) -> dict:
    """Blocking: real login + MQTT connect. Returns the credential state."""
    refresh_token, session = initial_login(email, password, sign_secrets=combined)
    mgr = EraSessionManager(sign_secrets=combined, email=email,
                            refresh_token=refresh_token, session=session)
    # short connect-only check (subscribes, no publish -> never actuates)
    CloudMqttLock(combined, mgr.mqtt_secrets()).watch(0.3, lambda *_: None)
    return {"refresh_token": refresh_token, "session": session}


async def _validate(hass: HomeAssistant, email: str, password: str,
                    combined: dict) -> dict:
    try:
        return await hass.async_add_executor_job(
            _validate_sync, email, password, combined)
    except EraAuthError as e:
        _LOGGER.debug("ERA auth failed: %s", e)
        raise _ValidationError("invalid_auth") from e
    except MqttUnlockError as e:
        _LOGGER.debug("Tuya MQTT connect failed: %s", e)
        raise _ValidationError("mqtt_failed") from e
    except _ValidationError:
        raise
    except Exception as e:  # noqa: BLE001
        _LOGGER.exception("Unexpected error validating ERA lock config")
        raise _ValidationError("unknown") from e


class EraLockConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ERA Protect Lock config + reauth flow."""

    VERSION = 1

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
                service = _parse(user_input[CONF_SERVICE_CONFIG],
                                 _SERVICE_REQUIRED, "invalid_service")
                secrets = _parse(user_input[CONF_SECRETS],
                                 _SECRETS_REQUIRED, "invalid_secrets")
                combined = {**service, **secrets}
                email = self._user[CONF_USERNAME]
                creds = await _validate(self.hass, email,
                                        self._user[CONF_PASSWORD], combined)
            except _ValidationError as e:
                errors["base"] = e.code
            else:
                await self.async_set_unique_id(str(combined["hub_id"]))
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
            combined = {**entry.data[CONF_SERVICE_CONFIG], **entry.data[CONF_SECRETS]}
            try:
                creds = await _validate(self.hass, email,
                                        user_input[CONF_PASSWORD], combined)
            except _ValidationError as e:
                errors["base"] = e.code
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_ERA: {"refresh_token": creds["refresh_token"], "email": email},
                        CONF_SESSION: creds["session"].to_dict(),
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"email": entry.data[CONF_ERA]["email"]},
        )
