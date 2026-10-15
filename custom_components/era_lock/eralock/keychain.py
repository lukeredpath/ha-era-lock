"""macOS Keychain storage for the Tuya session (via the `security` CLI).

We store ONLY the derived session material (token, ecode, chKey, uid, partner,
and the derived MQTT username/password/clientId) — never the user's Tuya
username or password. One generic-password item holds a JSON blob.
"""
from __future__ import annotations

import json
import subprocess
import sys

SERVICE = "era-lock"
ACCOUNT = "tuya-session"


class KeychainError(RuntimeError):
    pass


def _require_macos():
    if sys.platform != "darwin":
        raise KeychainError("Keychain storage requires macOS (`security` CLI).")


def save(data: dict, *, service: str = SERVICE, account: str = ACCOUNT) -> None:
    _require_macos()
    blob = json.dumps(data)
    # -U updates if the item already exists; -w passes the secret value.
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-a", account, "-s", service,
         "-D", "era-lock tuya session", "-w", blob],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise KeychainError(f"failed to store session in Keychain: {r.stderr.strip()}")


def load(*, service: str = SERVICE, account: str = ACCOUNT) -> dict | None:
    _require_macos()
    r = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None  # not found
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None


def delete(*, service: str = SERVICE, account: str = ACCOUNT) -> None:
    _require_macos()
    subprocess.run(
        ["security", "delete-generic-password", "-a", account, "-s", service],
        capture_output=True, text=True,
    )
