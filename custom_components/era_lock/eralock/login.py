"""Tuya mobile-API login — mint a fresh session offline.

Flow (reversed scheme, see research/FINDINGS.md):
  1. <prefix>.user.username.token.get  -> bootstrap token + RSA publicKey/exponent
  2. <prefix>.user.email.password.login -> sid, uid, partner...; ecode from a response header
Then eralock.session derives the MQTT creds from {uid, partner, chKey, sid(token), ecode}.

The user's email/password are used transiently to perform the login and are never
stored — only the derived session is persisted (Keychain). This mirrors a Home
Assistant config-flow + encrypted ConfigEntry.
"""
from __future__ import annotations

import hashlib
import time
import uuid

import requests
from cryptography.hazmat.primitives.asymmetric import padding as _pad
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

from .signing import TuyaSigner
from . import et3
from .secrets import load_sign_secrets
from .session import Session

API = "https://a1.tuyaeu.com/api.json"

# static request params for this app (config)
_APP = {
    "appVersion": "6.0.2",
    "os": "Android",
    "lang": "en_US",
    "ttid": "android",
    "et": "3",
}


class LoginError(RuntimeError):
    pass


class TuyaLogin:
    def __init__(self, *, sign_secrets: dict | None = None, country_code: str | None = None,
                 api_prefix: str = "smartlife.m", verbose: bool = False, tuid: str | None = None):
        self.sign = sign_secrets or load_sign_secrets()
        self.global_string = self.sign["hmac_key"]
        self.app_key = self.sign["clientId"]
        self.device_id = self.sign.get("device_id") or self._device_from_client_id()
        self.ch_key = self.sign["chKey"]            # install constant, in every request
        self.tuid = tuid or self.sign.get("tuid")   # from the ERA bridge, else config
        self.signer = TuyaSigner.from_secrets() if sign_secrets is None else \
            TuyaSigner(client_id=self.app_key, hmac_key=self.global_string)
        self.cc = country_code or self.sign.get("country_code", "+44")
        self.prefix = api_prefix
        self.verbose = verbose
        self.sess = requests.Session()
        self.ecode: str | None = None

    def _device_from_client_id(self) -> str:
        cid = self.sign.get("mqtt_client_id", "")
        parts = cid.split("_mb_")
        return parts[1].split("_")[0] if len(parts) > 1 else ""

    def call(self, api: str, v: str, post: dict, *, sid: str | None = None,
             ch_key: str | None = None) -> dict:
        rid = str(uuid.uuid4())
        params = dict(_APP)
        params.update({
            "a": api, "v": v, "clientId": self.app_key, "deviceId": self.device_id,
            "time": str(int(time.time())), "requestId": rid,
            "chKey": self.ch_key,          # constant, present in every request
        })
        if sid:
            params["sid"] = sid
        pd = et3.encrypt_body(post, rid, self.global_string, self.ecode)
        params["postData"] = pd
        params["sign"] = self.signer.sign_request(params, pd)
        r = self.sess.post(API, data=params, timeout=15,
                           headers={"User-Agent": "TY-UA=APP/Android/6.0.2"})
        # capture ecode from response header (binds per-session encryption)
        for h in ("ecode", "Ecode", "X-Tuya-Ecode"):
            if h in r.headers and not self.ecode:
                self.ecode = r.headers[h]
        if self.verbose:
            print(f"[{api}] http={r.status_code} ecode_hdr={self.ecode} "
                  f"body[:200]={r.text[:200]}")
        body = r.json()
        result = body.get("result")
        success = body.get("success")
        # encrypted responses: body.result is an et=3 b64 blob that decrypts to a
        # standard envelope {"result": <payload>, "success", "status", "t"}.
        if isinstance(result, str):
            try:
                env = et3.decrypt_body(result, rid, self.global_string, self.ecode)
                if isinstance(env, dict) and "result" in env:
                    result = env["result"]
                    success = env.get("success", success)
                else:
                    result = env
            except Exception as e:  # noqa: BLE001
                if self.verbose:
                    print("  (result not et3-decryptable:", e, ")")
        if success is False and self.verbose:
            print("  server error:", body.get("errorCode"), body.get("errorMsg"))
        return {"http": r, "body": body, "result": result, "success": success,
                "requestId": rid}

    def login(self, password: str) -> dict:
        """UID-based login (the app's re-login flow): token.get(uid) -> RSA password
        -> uid.password.login.reg. `password` is the ERA/Tuya account password; it is
        used transiently and never stored. Returns the raw responses + session fields."""
        # 1. bootstrap token + RSA pubkey (username = the stable tuid, isUid=true)
        tok = self.call(f"{self.prefix}.user.username.token.get", "2.0",
                        {"username": self.tuid, "countryCode": self.cc, "isUid": True})
        res = tok["result"] or {}
        token = res.get("token")
        exponent = res.get("exponent")
        pubkey = res.get("publicKey") or res.get("pbKey")
        if not (token and pubkey and exponent):
            raise LoginError(f"token.get did not return token/publicKey: {str(res)[:300]}")
        # 2. RSA-PKCS1v15( md5(password) ) with the server pubkey, then uid login
        pwd_md5 = hashlib.md5(password.encode()).hexdigest()
        pub = RSAPublicNumbers(int(exponent), int(pubkey)).public_key()
        passwd_enc = pub.encrypt(pwd_md5.encode("ascii"), _pad.PKCS1v15()).hex()
        login = self.call(f"{self.prefix}.user.uid.password.login.reg", "1.0",
                          {"uid": self.tuid, "countryCode": self.cc, "passwd": passwd_enc,
                           "ifencrypt": 1, "token": token, "createGroup": False,
                           "options": "{\"group\": 1}"})
        res = login["result"]
        if not (isinstance(res, dict) and res.get("sid")):
            err = res.get("errorMsg") if isinstance(res, dict) else res
            raise LoginError(f"login failed: {err}")
        session = Session(uid=res["uid"], partner=res["partnerIdentity"],
                          ch_key=self.ch_key, token=res["sid"], ecode=res["ecode"])
        return {"token_get": tok, "login": login, "session": session, "result": res}

    def login_session(self, password: str) -> Session:
        """Log in and return just the Session (the dynamic creds to persist)."""
        return self.login(password)["session"]
