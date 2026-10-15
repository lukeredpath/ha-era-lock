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

from dataclasses import dataclass

import boto3
import requests
from botocore import UNSIGNED
from botocore.config import Config

from .login import TuyaLogin
from .secrets import load_sign_secrets, SecretsError
from .session import Session

# Minimal GraphQL query — we only need the Tuya creds back.
_DEVICE_USER_QUERY = (
    "query DeviceUser($userName:String!,$deviceId:String!,$deviceType:String!)"
    "{deviceUser(userName:$userName,deviceId:$deviceId,deviceType:$deviceType)"
    "{dataList{tuid tpassword} response{code status description}}}"
)


class EraAuthError(RuntimeError):
    pass


@dataclass
class CognitoTokens:
    id_token: str
    refresh_token: str | None
    access_token: str | None = None
    expires_in: int | None = None


def _cognito_client(region: str):
    # public mobile client -> unsigned
    return boto3.client("cognito-idp", region_name=region,
                        config=Config(signature_version=UNSIGNED))


def cognito_login(username: str, password: str, *, client_id: str, region: str) -> CognitoTokens:
    """USER_PASSWORD_AUTH -> tokens.

    The refresh token lets the integration mint fresh idTokens later WITHOUT
    storing the password (verified 2026-10-15: refresh works password-free, no
    SECRET_HASH, idToken TTL 24h).
    """
    idp = _cognito_client(region)
    try:
        res = idp.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH", ClientId=client_id,
            AuthParameters={"USERNAME": username, "PASSWORD": password})
    except idp.exceptions.NotAuthorizedException:
        raise EraAuthError("incorrect ERA email or password")
    except Exception as e:  # noqa: BLE001
        raise EraAuthError(f"Cognito login failed: {e}")
    ar = res["AuthenticationResult"]
    return CognitoTokens(id_token=ar["IdToken"], refresh_token=ar.get("RefreshToken"),
                         access_token=ar.get("AccessToken"), expires_in=ar.get("ExpiresIn"))


def cognito_refresh(refresh_token: str, *, client_id: str, region: str) -> CognitoTokens:
    """REFRESH_TOKEN_AUTH -> fresh idToken, password-free.

    Raises EraAuthError if the refresh token is no longer valid; the caller
    should then trigger Home Assistant's reauth flow. The refresh token is reused
    (Cognito returns no new one here), so it is carried through unchanged.
    """
    idp = _cognito_client(region)
    try:
        res = idp.initiate_auth(
            AuthFlow="REFRESH_TOKEN_AUTH", ClientId=client_id,
            AuthParameters={"REFRESH_TOKEN": refresh_token})
    except idp.exceptions.NotAuthorizedException:
        raise EraAuthError("ERA session expired — please re-authenticate")
    except Exception as e:  # noqa: BLE001
        raise EraAuthError(f"Cognito refresh failed: {e}")
    ar = res["AuthenticationResult"]
    return CognitoTokens(id_token=ar["IdToken"],
                         refresh_token=ar.get("RefreshToken") or refresh_token,
                         access_token=ar.get("AccessToken"), expires_in=ar.get("ExpiresIn"))


def fetch_tuya_creds(id_token: str, username: str, *, appsync_url: str,
                     device_id: str, device_type: str) -> dict:
    r = requests.post(appsync_url, timeout=15,
                      headers={"Authorization": id_token, "Content-Type": "application/json"},
                      json={"query": _DEVICE_USER_QUERY,
                            "variables": {"userName": username, "deviceId": device_id,
                                          "deviceType": device_type}})
    j = r.json()
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
    r = requests.post(appsync_url, timeout=15,
                      headers={"Authorization": id_token, "Content-Type": "application/json"},
                      json={"query": _DOORLOCK_QUERY, "variables": {"userName": username}})
    j = r.json()
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
