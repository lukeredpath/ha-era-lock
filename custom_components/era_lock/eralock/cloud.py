"""Tuya mobile-API client for cloud-assisted unlock (in progress).

Request SIGNING is solved and offline (see signing.py). Two layers remain before
a standalone cloud unlock works, both requiring a capture while the owner is at
the machine (research/FINDINGS.md "NEXT"):

  1. Session: replicate the Tuya login (tuid/tpassword from era_keychain.json ->
     token/sid), so the integration can obtain/refresh a session unattended.
  2. Transport: every /api.json call uses et=3 — an ECDH-established AES channel
     (keyed by `chKey`) that encrypts postData and the response. This must be
     reproduced to read any response.
  3. The exact unlock request (api name + params) — learn it from one live
     capture under research/frida/frida_sign.js.

This module currently provides the request-building skeleton on top of the
proven signer; the networking methods are intentionally unimplemented until the
above are captured, so nothing here can actuate the lock yet.
"""
from __future__ import annotations

import time
import uuid

from .signing import TuyaSigner

API_HOST = "https://a1.tuyaeu.com/api.json"

# Common params observed on every signed request (research capture).
_COMMON = {
    "appVersion": "6.0.2",
    "os": "Android",
    "lang": "en_US",
    "ttid": "android",
    "v": "1.0",
    "et": "3",
}


class TuyaMobileClient:
    def __init__(self, signer: TuyaSigner, *, sid: str, device_id: str):
        self.signer = signer
        self.sid = sid
        self.device_id = device_id

    @classmethod
    def from_secrets(cls, *, sid: str, device_id: str) -> "TuyaMobileClient":
        return cls(TuyaSigner.from_secrets(), sid=sid, device_id=device_id)

    def build_request(self, api: str, *, post_data: str | None = None,
                      extra: dict | None = None) -> dict:
        """Assemble the full signed parameter dict for an /api.json call.

        Does NOT send anything. `post_data` must already be in the app's wire
        form (et=3 encryption not yet implemented — see module docstring).
        """
        params = dict(_COMMON)
        params.update({
            "a": api,
            "clientId": self.signer.client_id,
            "deviceId": self.device_id,
            "sid": self.sid,
            "time": str(int(time.time())),
            "requestId": str(uuid.uuid4()),
        })
        if post_data is not None:
            params["postData"] = post_data
        if extra:
            params.update(extra)
        params["sign"] = self.signer.sign_params(params)
        return params

    def call(self, api: str, **kw):  # pragma: no cover - not yet wired
        raise NotImplementedError(
            "Networking is gated on the login + et=3 transport capture; "
            "see cloud.py docstring and research/FINDINGS.md."
        )
