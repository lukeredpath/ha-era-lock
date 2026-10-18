"""ERA account bridge: ERA email+password -> Tuya session.

The ERA app (white-label Tuya) brokers Tuya access:
  1. Cognito USER_PASSWORD_AUTH(email, password) -> idToken
  2. AppSync GraphQL `DeviceUser(userName=email)` [Authorization: idToken]
         -> { tuid, tpassword }   (the Tuya account credentials)
  3. Tuya login(tuid, tpassword) -> Session   (see eralock.login)

So the user only ever provides their ERA email + password; the Tuya credentials
are fetched live and never exposed. Nothing here is stored — the caller persists
only the resulting Session (Keychain).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .login import TuyaLogin
from .secrets import load_sign_secrets, SecretsError
from .session import Session

# Minimal GraphQL query — we only need the Tuya creds back.
_DEVICE_USER_QUERY = (
    "query DeviceUser($userName:String!,$deviceId:String!,$deviceType:String!)"
    "{deviceUser(userName:$userName,deviceId:$deviceId,deviceType:$deviceType)"
    "{dataList{tuid tpassword} response{code status description}}}"
)


_LOGGER = logging.getLogger(__name__)

# (connect, read) seconds for the Cognito / AppSync HTTPS calls.
HTTP_TIMEOUT = (10, 20)

# Cognito error types (``__type``) that mean the CREDENTIALS were rejected —
# the password, or the refresh token (expired / revoked). Everything else
# Cognito reports is not a credential problem.
_COGNITO_AUTH_ERRORS = frozenset({
    "NotAuthorizedException",        # wrong password / bad or expired refresh token
    "UserNotFoundException",
    "UserNotConfirmedException",
    "PasswordResetRequiredException",
})
# Throttling: transient, like a transport failure.
_COGNITO_TRANSIENT_ERRORS = frozenset({
    "TooManyRequestsException",
    "LimitExceededException",
    "InternalErrorException",
})


class EraError(RuntimeError):
    """Base class for ERA bridge failures. Raised as-is for a request the
    service rejected for a reason that is neither the credentials nor the
    transport (e.g. a bad client id / region in the service config)."""


class EraAuthError(EraError):
    """The ERA account rejected the credentials (password or refresh token):
    user action (re-authentication) is required."""


class EraConnectionError(EraError):
    """The ERA service couldn't be reached or answered unusably (DNS, timeout,
    TLS, 5xx, throttling, non-JSON): transient — retry later, don't reauth."""


@dataclass
class CognitoTokens:
    id_token: str
    refresh_token: str | None
    access_token: str | None = None
    expires_in: int | None = None


def _initiate_auth(flow: str, params: dict, *, client_id: str, region: str,
                   rejected: str) -> dict:
    """Cognito ``InitiateAuth`` over plain HTTPS (the AWS JSON 1.1 protocol).

    The ERA app's user-pool client is a public mobile client: no SECRET_HASH
    and no SigV4 — the request is unsigned, so no AWS SDK is needed. Returns
    the ``AuthenticationResult`` dict. NEVER log the request body: it carries
    the password / refresh token.

    Raises EraAuthError (credentials rejected; message ``rejected``),
    EraConnectionError (transport, 5xx, throttling, unparseable answer) or
    EraError (any other rejection, e.g. a bad client id)."""
    url = f"https://cognito-idp.{region}.amazonaws.com/"
    headers = {
        "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        "Content-Type": "application/x-amz-json-1.1",
    }
    body = {"AuthFlow": flow, "ClientId": client_id, "AuthParameters": params}
    try:
        r = requests.post(url, json=body, headers=headers, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise EraConnectionError(
            f"Cognito {flow} request failed: {type(e).__name__}") from e
    try:
        j = r.json()
    except ValueError:
        j = None
    status = r.status_code
    if status == 200 and isinstance(j, dict):
        ar = j.get("AuthenticationResult")
        if isinstance(ar, dict) and ar.get("IdToken"):
            return ar
        if j.get("ChallengeName"):
            # Credentials accepted but the account needs an extra step (e.g.
            # NEW_PASSWORD_REQUIRED) that only the ERA app can complete: user
            # action, same remedy as a rejection.
            raise EraAuthError(
                f"ERA account requires {j['ChallengeName']} — sign in to the ERA "
                "app to complete it")
        raise EraConnectionError("Cognito returned no AuthenticationResult")
    err_type = ""
    message = ""
    if isinstance(j, dict):
        err_type = str(j.get("__type") or "")
        message = str(j.get("message") or j.get("Message") or "")
    err_type = (err_type or r.headers.get("x-amzn-ErrorType", "")).split(":")[0]
    err_type = err_type.rsplit("#", 1)[-1]  # "com.amazonaws...#NotAuthorized..."
    _LOGGER.debug("Cognito %s failed: HTTP %s %s", flow, status, err_type or "-")
    if status >= 500 or err_type in _COGNITO_TRANSIENT_ERRORS or status == 429:
        raise EraConnectionError(f"Cognito unavailable (HTTP {status} {err_type})".strip())
    if err_type in _COGNITO_AUTH_ERRORS:
        raise EraAuthError(f"{rejected} ({err_type})")
    if not err_type:
        # Not a Cognito error document (a proxy / captive portal page, ...).
        raise EraConnectionError(f"unexpected Cognito response (HTTP {status})")
    raise EraError(f"Cognito rejected the request: {err_type}"
                   + (f" ({message})" if message else ""))


def cognito_login(username: str, password: str, *, client_id: str, region: str) -> CognitoTokens:
    """USER_PASSWORD_AUTH -> tokens.

    The refresh token lets the integration mint fresh idTokens later WITHOUT
    storing the password (verified 2026-10-15: refresh works password-free, no
    SECRET_HASH, idToken TTL 24h).
    """
    ar = _initiate_auth("USER_PASSWORD_AUTH",
                        {"USERNAME": username, "PASSWORD": password},
                        client_id=client_id, region=region,
                        rejected="incorrect ERA email or password")
    return CognitoTokens(id_token=ar["IdToken"], refresh_token=ar.get("RefreshToken"),
                         access_token=ar.get("AccessToken"), expires_in=ar.get("ExpiresIn"))


def cognito_refresh(refresh_token: str, *, client_id: str, region: str) -> CognitoTokens:
    """REFRESH_TOKEN_AUTH -> fresh idToken, password-free.

    Raises EraAuthError if the refresh token is no longer valid (the caller
    should then trigger Home Assistant's reauth flow), EraConnectionError if
    Cognito couldn't be reached (retry; NOT a reason to reauth). The refresh
    token is reused (Cognito returns no new one here), so it is carried through
    unchanged.
    """
    ar = _initiate_auth("REFRESH_TOKEN_AUTH", {"REFRESH_TOKEN": refresh_token},
                        client_id=client_id, region=region,
                        rejected="ERA session expired — please re-authenticate")
    return CognitoTokens(id_token=ar["IdToken"],
                         refresh_token=ar.get("RefreshToken") or refresh_token,
                         access_token=ar.get("AccessToken"), expires_in=ar.get("ExpiresIn"))


def _appsync(appsync_url: str, id_token: str, payload: dict) -> dict:
    """POST a GraphQL query to ERA's AppSync; transport / non-JSON failures
    raise EraConnectionError."""
    try:
        r = requests.post(appsync_url, timeout=HTTP_TIMEOUT,
                          headers={"Authorization": id_token,
                                   "Content-Type": "application/json"},
                          json=payload)
    except requests.RequestException as e:
        raise EraConnectionError(f"AppSync request failed: {type(e).__name__}") from e
    if r.status_code >= 500:
        raise EraConnectionError(f"AppSync unavailable (HTTP {r.status_code})")
    try:
        j = r.json()
    except ValueError as e:
        raise EraConnectionError(
            f"unexpected AppSync response (HTTP {r.status_code})") from e
    if not isinstance(j, dict):
        raise EraConnectionError(f"unexpected AppSync response (HTTP {r.status_code})")
    return j


def fetch_tuya_creds(id_token: str, username: str, *, appsync_url: str,
                     device_id: str, device_type: str) -> dict:
    j = _appsync(appsync_url, id_token,
                 {"query": _DEVICE_USER_QUERY,
                  "variables": {"userName": username, "deviceId": device_id,
                                "deviceType": device_type}})
    if j.get("errors"):
        raise EraAuthError(f"AppSync DeviceUser error: {j['errors']}")
    data = (((j.get("data") or {}).get("deviceUser") or {}).get("dataList")) or []
    if not data or not data[0].get("tuid") or not data[0].get("tpassword"):
        raise EraAuthError("AppSync DeviceUser returned no tuid/tpassword")
    return {"tuid": data[0]["tuid"], "tpassword": data[0]["tpassword"]}


_DOORLOCK_QUERY = (
    "query GetDoorlockByUser($userName:String!){getDoorlockByUser(userName:$userName)"
    "{response{code status description} dataList{doorlockName mac mode battery "
    "remoteUnlockEnabled tamperDetected fpcLocked bleDisabled syncLogs firmwareVersion}}}"
)


def fetch_doorlocks(id_token: str, username: str, *, appsync_url: str) -> list:
    """getDoorlockByUser -> lock metadata (name/flags/firmware). NOTE: `mode` and
    `battery` are cloud-stale — use local dp8 for battery and the inference model
    for state. Useful for device info / firmware."""
    j = _appsync(appsync_url, id_token,
                 {"query": _DOORLOCK_QUERY, "variables": {"userName": username}})
    if j.get("errors"):
        raise EraAuthError(f"getDoorlockByUser error: {j['errors']}")
    return (((j.get("data") or {}).get("getDoorlockByUser") or {}).get("dataList")) or []


def login(username: str, password: str, *, sign_secrets: dict | None = None) -> Session:
    """ERA email+password -> fresh Tuya Session. Credentials are used transiently."""
    sign = sign_secrets or load_sign_secrets()
    for key in ("cognito_client_id", "cognito_region", "appsync_url"):
        if not sign.get(key):
            raise SecretsError(f"sign_secrets.json missing '{key}' (ERA bridge config)")
    id_token = cognito_login(username, password,
                             client_id=sign["cognito_client_id"],
                             region=sign["cognito_region"]).id_token
    creds = fetch_tuya_creds(id_token, username, appsync_url=sign["appsync_url"],
                             device_id=sign.get("device_id", "era-lock-cli"),
                             device_type=sign.get("era_device_type", "era-lock-cli"))
    # Tuya login with the freshly-fetched tuid/tpassword
    tl = TuyaLogin(sign_secrets=sign, tuid=creds["tuid"])
    return tl.login_session(creds["tpassword"])
