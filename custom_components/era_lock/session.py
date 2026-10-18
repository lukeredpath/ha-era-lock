"""Credential/session lifecycle for the integration.

Wraps the eralock library's auth primitives into a small manager that the
component drives. Everything here is SYNCHRONOUS (plain HTTPS via requests under the
hood); the component calls these methods via ``hass.async_add_executor_job`` and
persists any changed credentials back to the config entry.

Credential model (validated 2026-10-15):
  - Store the Cognito **refresh token**, never the password.
  - idTokens last ~24h and are minted on demand via REFRESH_TOKEN_AUTH.
  - The Tuya session (token/ecode) is refreshed by re-running AppSync→Tuya-login,
    which only needs a fresh idToken. MQTT connect creds derive from that session.
  - When the refresh token itself lapses, ``EraAuthError`` propagates and the
    component raises Home Assistant's reauth flow.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .eralock.era import (
    EraAuthError,
    EraConnectionError,
    cognito_login,
    cognito_refresh,
    fetch_tuya_creds,
)
from .eralock.login import TuyaLogin
from .eralock.session import Session, derive_mqtt_secrets

_LOGGER = logging.getLogger(__name__)

# Keys we read out of the pasted sign_secrets blob to drive the ERA bridge.
_ERA_CFG_KEYS = ("cognito_client_id", "cognito_region", "appsync_url")


def era_cfg_from_sign(sign_secrets: dict) -> dict:
    """Pull the (non-secret) ERA bridge config out of the sign_secrets blob."""
    cfg = {k: sign_secrets.get(k) for k in _ERA_CFG_KEYS}
    cfg["device_id"] = sign_secrets.get("device_id", "era-lock-cli")
    cfg["era_device_type"] = sign_secrets.get("era_device_type", "era-lock-cli")
    return cfg


def initial_login(email: str, password: str, *, sign_secrets: dict) -> tuple[str, Session]:
    """ERA email+password -> (refresh_token, Tuya Session). Used by the config flow.

    Mirrors ``eralock.era.login`` but also captures the refresh token so the
    integration never has to store the password.
    """
    era = era_cfg_from_sign(sign_secrets)
    toks = cognito_login(email, password,
                         client_id=era["cognito_client_id"], region=era["cognito_region"])
    if not toks.refresh_token:
        raise EraAuthError("Cognito returned no refresh token")
    creds = fetch_tuya_creds(toks.id_token, email, appsync_url=era["appsync_url"],
                             device_id=era["device_id"], device_type=era["era_device_type"])
    session = TuyaLogin(sign_secrets=sign_secrets, tuid=creds["tuid"]).login_session(
        creds["tpassword"])
    return toks.refresh_token, session


@dataclass
class EraSessionManager:
    """Holds and refreshes ERA/Tuya credentials. Hass-free and synchronous."""

    sign_secrets: dict
    email: str
    refresh_token: str
    session: Session
    _id_token: str | None = None
    _id_token_exp: float = 0.0
    # Called (on the calling thread — paho / executor) with
    # ``entry_credentials()`` after every successful re-mint, so the component
    # can persist them to the config entry. Must not raise / block.
    on_credentials_changed: Callable[[dict], None] | None = field(
        default=None, repr=False, compare=False)

    @property
    def era(self) -> dict:
        return era_cfg_from_sign(self.sign_secrets)

    def ensure_id_token(self, *, skew: float = 120.0) -> str:
        """Return a valid idToken, refreshing via REFRESH_TOKEN_AUTH if needed."""
        if self._id_token and time.time() < self._id_token_exp - skew:
            return self._id_token
        toks = cognito_refresh(self.refresh_token,
                               client_id=self.era["cognito_client_id"],
                               region=self.era["cognito_region"])
        self._id_token = toks.id_token
        self._id_token_exp = time.time() + (toks.expires_in or 3600)
        self.refresh_token = toks.refresh_token or self.refresh_token
        return self._id_token

    def refresh_tuya_session(self) -> Session:
        """Re-mint the Tuya session (AppSync tpassword -> Tuya login).

        Error contract (the listener latches + starts reauth on EraAuthError
        only): ``EraAuthError`` means the ACCOUNT rejected us — the Cognito
        refresh token is expired/revoked, which only re-entering the password
        fixes. An AppSync rejection is retried once with a freshly refreshed
        idToken (a cached one may have been revoked); if Cognito accepts us
        but AppSync still refuses, a new password can't help (reauth re-runs
        the same chain), so it is reported as ``EraConnectionError`` and
        retried with backoff rather than parking the integration on a reauth.
        Tuya login failures likewise propagate as non-auth errors."""
        era = self.era

        def fetch(id_token: str) -> dict:
            return fetch_tuya_creds(id_token, self.email, appsync_url=era["appsync_url"],
                                    device_id=era["device_id"],
                                    device_type=era["era_device_type"])

        id_token = self.ensure_id_token()
        try:
            creds = fetch(id_token)
        except EraAuthError:
            self._id_token = None
            id_token = self.ensure_id_token()  # EraAuthError here is genuine
            try:
                creds = fetch(id_token)
            except EraAuthError as e:
                raise EraConnectionError(
                    f"ERA AppSync refused a freshly refreshed sign-in: {e}") from e
        self.session = TuyaLogin(sign_secrets=self.sign_secrets,
                                 tuid=creds["tuid"]).login_session(creds["tpassword"])
        cb = self.on_credentials_changed
        if cb is not None:
            try:
                cb(self.entry_credentials())
            except Exception:  # noqa: BLE001 — persistence must never break a re-mint
                _LOGGER.exception(
                    "ERA: could not hand re-minted credentials to the config entry")
        return self.session

    def mqtt_secrets(self) -> dict:
        """Derive the Tuya MQTT connect creds from the current session."""
        return derive_mqtt_secrets(self.session, sign_secrets=self.sign_secrets)

    def entry_credentials(self) -> dict:
        """The mutable credential state to persist back to the config entry."""
        return {"refresh_token": self.refresh_token, "session": self.session.to_dict()}
