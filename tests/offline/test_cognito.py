"""Offline tests for the boto3-free Cognito InitiateAuth (eralock/era.py) and
its error mapping, with a fake ``requests`` (no network). Stdlib only.

Run (from the repo root):  python3 -I -B tests/offline/test_cognito.py
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import types

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG_DIR = os.path.abspath(
    (sys.argv[1] if len(sys.argv) > 1 else None) or os.environ.get("ERA_LOCK_PKG")
    or os.path.join(_REPO, "custom_components", "era_lock"))

# Obviously fake identifiers.
CLIENT_ID = "fakeclientid000000000000"
REGION = "eu-west-1"
PASSWORD = "not-a-real-password"
REFRESH = "fake-refresh-token"


# ------------------------------------------------------------ fake requests --
class RequestException(Exception):
    pass


class ConnectionError_(RequestException):
    pass


class Timeout(RequestException):
    pass


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        return json.loads(self._body)  # raises ValueError for non-JSON


CALLS: list = []
NEXT: list = []  # FakeResponse or exception to return/raise per call


def fake_post(url, data=None, json=None, headers=None, timeout=None):  # noqa: A002
    CALLS.append({"url": url, "json": json, "data": data, "headers": dict(headers or {}),
                  "timeout": timeout})
    nxt = NEXT.pop(0)
    if isinstance(nxt, Exception):
        raise nxt
    return nxt


fake_requests = types.ModuleType("requests")
fake_requests.post = fake_post
fake_requests.RequestException = RequestException
fake_requests.ConnectionError = ConnectionError_
fake_requests.Timeout = Timeout
sys.modules["requests"] = fake_requests

# Load the REAL eralock/era.py; stub its sibling modules (Tuya login etc.).
pkg = types.ModuleType("era_lock")
pkg.__path__ = [PKG_DIR]
sys.modules["era_lock"] = pkg
sub = types.ModuleType("era_lock.eralock")
sub.__path__ = [PKG_DIR + "/eralock"]
sys.modules["era_lock.eralock"] = sub
for name, attrs in {
    "login": {"TuyaLogin": object},
    "secrets": {"load_sign_secrets": dict, "SecretsError": type("SecretsError", (Exception,), {})},
    "session": {"Session": object},
}.items():
    m = types.ModuleType(f"era_lock.eralock.{name}")
    m.__dict__.update(attrs)
    sys.modules[m.__name__] = m
era = importlib.import_module("era_lock.eralock.era")

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


def expect(fn, exc):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return type(e) is exc, e
    return False, None


def login():
    return era.cognito_login("someone@example.invalid", PASSWORD,
                             client_id=CLIENT_ID, region=REGION)


def refresh():
    return era.cognito_refresh(REFRESH, client_id=CLIENT_ID, region=REGION)


OK_LOGIN = {"AuthenticationResult": {
    "IdToken": "id.tok.en", "RefreshToken": "new-refresh", "AccessToken": "acc",
    "ExpiresIn": 86400, "TokenType": "Bearer"}, "ChallengeParameters": {}}


def t_request_shape():
    CALLS.clear(); NEXT[:] = [FakeResponse(200, OK_LOGIN)]
    toks = login()
    c = CALLS[0]
    check("cognito: POST to the regional Cognito IdP endpoint",
          c["url"] == f"https://cognito-idp.{REGION}.amazonaws.com/", c["url"])
    check("cognito: X-Amz-Target InitiateAuth + amz-json-1.1 content type",
          c["headers"].get("X-Amz-Target")
          == "AWSCognitoIdentityProviderService.InitiateAuth"
          and c["headers"].get("Content-Type") == "application/x-amz-json-1.1",
          c["headers"])
    check("cognito: USER_PASSWORD_AUTH body (no SECRET_HASH)",
          c["json"] == {"AuthFlow": "USER_PASSWORD_AUTH", "ClientId": CLIENT_ID,
                        "AuthParameters": {"USERNAME": "someone@example.invalid",
                                           "PASSWORD": PASSWORD}}, c["json"])
    check("cognito: a timeout is always set", c["timeout"], c["timeout"])
    check("cognito: tokens parsed from AuthenticationResult",
          toks.id_token == "id.tok.en" and toks.refresh_token == "new-refresh"
          and toks.access_token == "acc" and toks.expires_in == 86400, toks)

    CALLS.clear(); NEXT[:] = [FakeResponse(200, {"AuthenticationResult": {
        "IdToken": "id2", "AccessToken": "a2", "ExpiresIn": 3600}})]
    toks = refresh()
    c = CALLS[0]
    check("cognito: REFRESH_TOKEN_AUTH body",
          c["json"] == {"AuthFlow": "REFRESH_TOKEN_AUTH", "ClientId": CLIENT_ID,
                        "AuthParameters": {"REFRESH_TOKEN": REFRESH}}, c["json"])
    check("cognito: refresh keeps the old refresh token when none is returned",
          toks.id_token == "id2" and toks.refresh_token == REFRESH
          and toks.expires_in == 3600, toks)


def t_error_mapping():
    def err(status, typ, msg="m", headers=None):
        return FakeResponse(status, {"__type": typ, "message": msg}, headers)

    cases = [
        ("NotAuthorizedException (login) -> EraAuthError", login,
         err(400, "NotAuthorizedException", "Incorrect username or password."),
         era.EraAuthError),
        ("NotAuthorizedException (refresh expired) -> EraAuthError", refresh,
         err(400, "NotAuthorizedException", "Refresh Token has expired"),
         era.EraAuthError),
        ("UserNotFoundException -> EraAuthError", login,
         err(400, "UserNotFoundException"), era.EraAuthError),
        ("namespaced __type still recognised", login,
         err(400, "com.amazonaws.cognito#NotAuthorizedException"), era.EraAuthError),
        ("type only in x-amzn-ErrorType header", login,
         FakeResponse(400, {"message": "x"},
                      {"x-amzn-ErrorType": "NotAuthorizedException:http://x"}),
         era.EraAuthError),
        ("challenge (NEW_PASSWORD_REQUIRED) -> EraAuthError", login,
         FakeResponse(200, {"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "s"}),
         era.EraAuthError),
        ("TooManyRequestsException -> EraConnectionError", login,
         err(400, "TooManyRequestsException"), era.EraConnectionError),
        ("InternalErrorException (500) -> EraConnectionError", refresh,
         err(500, "InternalErrorException"), era.EraConnectionError),
        ("503 non-JSON -> EraConnectionError", refresh,
         FakeResponse(503, "<html>Service Unavailable</html>"), era.EraConnectionError),
        ("200 non-JSON (captive portal) -> EraConnectionError", login,
         FakeResponse(200, "<html>login to wifi</html>"), era.EraConnectionError),
        ("400 non-JSON -> EraConnectionError", login,
         FakeResponse(400, "bad gateway-ish"), era.EraConnectionError),
        ("DNS/connection failure -> EraConnectionError", login,
         ConnectionError_("Name or service not known"), era.EraConnectionError),
        ("timeout -> EraConnectionError", refresh, Timeout("read timed out"),
         era.EraConnectionError),
        ("ResourceNotFoundException (bad client id) -> plain EraError", login,
         err(400, "ResourceNotFoundException", "User pool client x does not exist."),
         era.EraError),
        ("InvalidParameterException (flow disabled) -> plain EraError", login,
         err(400, "InvalidParameterException"), era.EraError),
    ]
    for name, fn, resp, exc in cases:
        CALLS.clear(); NEXT[:] = [resp]
        ok, e = expect(fn, exc)
        check(f"cognito: {name}", ok, repr(e))
        if e is not None:
            check(f"cognito: ...message never echoes the password / refresh token",
                  PASSWORD not in str(e) and REFRESH not in str(e), str(e))
    check("cognito: hierarchy — auth/connection are EraErrors, distinct",
          issubclass(era.EraAuthError, era.EraError)
          and issubclass(era.EraConnectionError, era.EraError)
          and not issubclass(era.EraConnectionError, era.EraAuthError)
          and not issubclass(era.EraAuthError, era.EraConnectionError))


def t_appsync_transport():
    CALLS.clear(); NEXT[:] = [Timeout("t")]
    ok, e = expect(lambda: era.fetch_tuya_creds(
        "id", "u", appsync_url="https://example.invalid/graphql",
        device_id="d", device_type="t"), era.EraConnectionError)
    check("appsync: timeout -> EraConnectionError", ok, repr(e))
    CALLS.clear(); NEXT[:] = [FakeResponse(502, "<html>")]
    ok, e = expect(lambda: era.fetch_tuya_creds(
        "id", "u", appsync_url="https://example.invalid/graphql",
        device_id="d", device_type="t"), era.EraConnectionError)
    check("appsync: 502 -> EraConnectionError", ok, repr(e))
    CALLS.clear(); NEXT[:] = [FakeResponse(200, {"data": {"deviceUser": {
        "dataList": [{"tuid": "fake-tuid", "tpassword": "fake-tpw"}]}}})]
    got = era.fetch_tuya_creds("id", "u", appsync_url="https://example.invalid/graphql",
                               device_id="d", device_type="t")
    check("appsync: success unchanged",
          got == {"tuid": "fake-tuid", "tpassword": "fake-tpw"}
          and CALLS[0]["timeout"], got)


def t_no_boto():
    src = open(os.path.join(PKG_DIR, "eralock", "era.py")).read()
    man = json.load(open(os.path.join(PKG_DIR, "manifest.json")))
    check("no boto3/botocore import in era.py; not in manifest requirements",
          "boto" not in src and not any("boto" in r for r in man["requirements"]),
          man["requirements"])
    check("boto3/botocore never imported", "boto3" not in sys.modules
          and "botocore" not in sys.modules)


if __name__ == "__main__":
    for fn in (t_request_shape, t_error_mapping, t_appsync_transport, t_no_boto):
        print(f"--- {fn.__name__}")
        fn()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
