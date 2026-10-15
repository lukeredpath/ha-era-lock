"""Derive the Tuya MQTT connect credentials from a session, fully offline.

VERIFIED against live captures (research/FINDINGS.md "SESSION/CRED DERIVATION
CRACKED"); the values the native lib computes are double-MD5 over material we hold:

    password = md5( md5(GLOBAL_STRING) + ecode )[8:24]
    suffix   = md5( md5(appKey) + ecode )[-16:]
    username = f"{partner}_v1_{appKey}_{chKey}_mb_{token}{suffix}"
    clientId = install-stable constant (from config)

Static inputs (GLOBAL_STRING/appKey/clientId/deviceId) come from gitignored config
(sign_secrets.json / lock_secrets.json). The dynamic session (token/ecode/chKey/
uid/partner) is obtained at login and kept in the Keychain — never username/password.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import keychain
from .secrets import load_sign_secrets, SecretsError

BROKER = "ssl://m1.tuyaeu.com:8883"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def mqtt_password(global_string: str, ecode: str) -> str:
    return _md5(_md5(global_string) + ecode)[8:24]


def username_suffix(app_key: str, ecode: str) -> str:
    return _md5(_md5(app_key) + ecode)[-16:]


def mqtt_username(partner: str, app_key: str, ch_key: str, token: str, ecode: str) -> str:
    return f"{partner}_v1_{app_key}_{ch_key}_mb_{token}{username_suffix(app_key, ecode)}"


@dataclass
class Session:
    uid: str
    partner: str
    ch_key: str
    token: str
    ecode: str

    def to_dict(self) -> dict:
        return {"uid": self.uid, "partner": self.partner, "ch_key": self.ch_key,
                "token": self.token, "ecode": self.ecode}

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(uid=d["uid"], partner=d["partner"], ch_key=d["ch_key"],
                   token=d["token"], ecode=d["ecode"])


def derive_mqtt_secrets(session: Session, *, sign_secrets: dict | None = None) -> dict:
    """Build the mqtt_secrets dict (broker/clientId/username/password) from a session.

    `clientId` is an install-stable constant read from sign_secrets.json
    ("mqtt_client_id"); everything else is derived from the session + GLOBAL_STRING.
    """
    sign = sign_secrets or load_sign_secrets()
    global_string = sign["hmac_key"]
    app_key = sign["clientId"]
    client_id = sign.get("mqtt_client_id")
    if not client_id:
        raise SecretsError(
            "sign_secrets.json missing 'mqtt_client_id' (install-stable MQTT clientId; "
            "capture once, e.g. research/frida/frida_mqtt_creds.js).")
    return {
        "broker": BROKER,
        "clientId": client_id,
        "username": mqtt_username(session.partner, app_key, session.ch_key,
                                  session.token, session.ecode),
        "password": mqtt_password(global_string, session.ecode),
        "keepAlive": 60,
        "cleanSession": True,
    }


# ---- Keychain-backed session persistence (dynamic values only) --------------

def save_session(session: Session) -> None:
    keychain.save(session.to_dict())


def load_session() -> Session | None:
    d = keychain.load()
    if not d:
        return None
    try:
        return Session.from_dict(d)
    except KeyError:
        return None


def clear_session() -> None:
    keychain.delete()
