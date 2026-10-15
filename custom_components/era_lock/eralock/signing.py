"""Offline Tuya mobile-API request signing for the ERA app.

The app signs every /api.json request with HMAC-SHA256 over an alphabetically
sorted, '||'-joined query string. The HMAC key is a static per-app-version
secret recovered by reverse-engineering (research/FINDINGS.md "SIGNING CRACKED"):

    key = package + "_" + UPPER_COLON(sha256(signing cert)) + "_" + bmpToken + "_" + appSecret

It is not derivable from public data, so it is loaded from sign_secrets.json
(gitignored) rather than embedded here. Validated 37/37 against a live capture.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from .secrets import load_sign_secrets


@dataclass
class TuyaSigner:
    client_id: str
    hmac_key: str

    @classmethod
    def from_secrets(cls, path=None) -> "TuyaSigner":
        d = load_sign_secrets(path)
        key = d.get("hmac_key")
        if not key:  # assemble from parts so the formula stays auditable
            key = "_".join([d["package"], d["cert_sha256_colon_upper"],
                            d["bmp_token"], d["appSecret"]])
        return cls(client_id=d["clientId"], hmac_key=key)

    def build_sign_input(self, params: dict) -> str:
        """The alphabetically sorted, '||'-joined string the app signs
        (all request params except 'sign' itself)."""
        items = sorted((k, v) for k, v in params.items() if k != "sign")
        return "||".join(f"{k}={v}" for k, v in items)

    def sign_string(self, sign_input: str) -> str:
        return hmac.new(self.hmac_key.encode(), sign_input.encode(),
                        hashlib.sha256).hexdigest()

    def sign_params(self, params: dict) -> str:
        return self.sign_string(self.build_sign_input(params))

    # -- request BUILD mode (for new requests; verified scheme) ---------------
    def sign_request(self, params: dict, post_data_b64: str | None = None) -> str:
        """Sign a request being BUILT: filter to SIGN_FIELDS, replace postData with
        swap(md5(postData_b64)), sort, '||'-join, HMAC. The actual POST still sends
        the raw postData_b64; only the SIGN uses the swapped md5."""
        p = {k: str(v) for k, v in params.items()
             if k in SIGN_FIELDS and v is not None and str(v) != ""}
        if post_data_b64 is not None:
            p["postData"] = swap_md5(post_data_b64)
        return self.sign_string(self.build_sign_input(p))


# Fields included in the signature (ThingApiSignManager), per the reversed scheme.
SIGN_FIELDS = {
    "a", "v", "lat", "lon", "lang", "deviceId", "appVersion", "ttid", "isH5",
    "h5Token", "os", "clientId", "postData", "time", "requestId", "et", "n4h5",
    "sid", "chKey", "sp",
}


def swap_md5(b64: str) -> str:
    """Tuya's swapSignString shuffle of md5(postData_b64): 8-char chunks [1][0][3][2]."""
    h = hashlib.md5(b64.encode()).hexdigest()
    return h[8:16] + h[0:8] + h[24:32] + h[16:24]
