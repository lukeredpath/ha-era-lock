"""Tuya mobile-API et=3 transport (AES-128-GCM) + request signing helpers.

Per-request key is deterministic from the requestId (UUID4) and the session ecode
and the app's GLOBAL_STRING (= the signing hmac_key). Verified scheme (see
research/FINDINGS.md "SESSION/CRED DERIVATION CRACKED"):

    key   = hmac_sha256(requestId, GLOBAL_STRING + ("_"+ecode if ecode else "")).hexdigest()[:16]  (ASCII)
    body  = base64( nonce[12] + AESGCM(key).encrypt(nonce, json) )   # ct includes the 16-byte tag
    resp  = AESGCM(key).decrypt(nonce=raw[:12], raw[12:])            # gzip if x-content-compress: gzip
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encrypto_key(request_id: str, global_string: str, ecode: str | None) -> bytes:
    suffix = ("_" + ecode) if ecode else ""
    digest = hmac.new(request_id.encode(), (global_string + suffix).encode(),
                      hashlib.sha256).hexdigest()
    return digest[:16].encode("ascii")


def encrypt_body(payload: dict, request_id: str, global_string: str, ecode: str | None) -> str:
    key = encrypto_key(request_id, global_string, ecode)
    nonce = os.urandom(12)
    pt = json.dumps(payload, separators=(",", ":")).encode()
    ct = AESGCM(key).encrypt(nonce, pt, None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_body(b64_data: str, request_id: str, global_string: str, ecode: str | None,
                 *, gzipped: bool = False) -> dict:
    key = encrypto_key(request_id, global_string, ecode)
    raw = base64.b64decode(b64_data)
    pt = AESGCM(key).decrypt(raw[:12], raw[12:], None)
    if gzipped:
        pt = gzip.decompress(pt)
    return json.loads(pt)
