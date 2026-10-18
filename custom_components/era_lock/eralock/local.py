"""Local LAN control/read for the ERA lock over the Tuya Wi-Fi hub.

The lock is a BLE sub-device of the hub, addressed by its `cid`, so traffic goes:
  caller -> hub (<hub_ip>:6668, Tuya v3.4) -> BLE -> lock.

Reads work fully (battery, unlock events, enrolled-method attribution). Writing
the unlock DP is accepted by the gateway but does NOT actuate (cloud-ticket-
gated) — real unlock is the cloud path (see cloud.py). True lock state is not
reported by the hardware, so it is inferred best-effort from unlock events + the
auto-relock window (see FINDINGS.md "HA STATUS DESIGN").
"""
from __future__ import annotations

import json
import time

import tinytuya

from . import dpmap
from .secrets import STATE_PATH, SecretsError

DEFAULT_RELOCK_SECONDS = 30


class LocalLock:
    def __init__(self, secrets: dict):
        self.secrets = secrets
        self._dev: tinytuya.Device | None = None

    # -- connection -----------------------------------------------------------
    def connect(self, persist: bool = False) -> tinytuya.Device:
        s = self.secrets
        version = float(s.get("version", 3.4))
        cid = s.get("lock_cid")
        if cid:
            gateway = tinytuya.Device(
                dev_id=s["hub_id"], address=s["hub_ip"], local_key=s["local_key"],
                version=version, persist=persist,
            )
            dev = tinytuya.Device(
                dev_id=s.get("lock_id", s["hub_id"]), cid=cid, parent=gateway,
            )
        else:
            dev = tinytuya.Device(
                dev_id=s["hub_id"], address=s["hub_ip"], local_key=s["local_key"],
                version=version, persist=persist,
            )
        self._dev = dev
        return dev

    # -- reads ----------------------------------------------------------------
    def raw_status(self, dev: tinytuya.Device | None = None) -> dict:
        dev = dev or self._dev or self.connect()
        data = dev.status()
        if not isinstance(data, dict):
            raise SecretsError(f"unexpected status response: {data!r}")
        if "Error" in data:
            err = str(data.get("Err"))
            hint = {"914": "wrong or empty localKey",
                    "901": "could not connect / wrong address"}.get(err, "")
            raise SecretsError(
                f"Tuya error {err}: {data.get('Error')}" + (f" ({hint})" if hint else "")
            )
        return data

    def dps(self) -> dict:
        return self.raw_status().get("dps", {})

    def battery(self) -> int | None:
        v = self.dps().get(str(dpmap.BATTERY_DP))
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    # -- unlock-event inference cache ----------------------------------------
    @staticmethod
    def _load_state() -> dict:
        try:
            with open(STATE_PATH) as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _save_state(state: dict) -> None:
        try:
            with open(STATE_PATH, "w") as fh:
                json.dump(state, fh)
        except OSError:
            pass  # best-effort; never fail a command over the cache

    def record_unlock(self) -> None:
        state = self._load_state()
        state["last_unlock_ts"] = time.time()
        self._save_state(state)

    def inferred_state(self) -> dict:
        """Return {'state': ..., 'detail': ..., 'authoritative': bool}.

        Authoritative if a real status/door DP is configured; else inferred from
        the last observed unlock event and the auto-relock window.
        """
        s = self.secrets
        dps = self.dps()
        status_dp, door_dp = s.get("status_dp"), s.get("door_dp")
        if status_dp is not None and str(status_dp) in dps:
            return {"state": dps[str(status_dp)], "detail": f"dp {status_dp}",
                    "authoritative": True}
        if door_dp is not None and str(door_dp) in dps:
            return {"state": dps[str(door_dp)], "detail": f"door contact dp {door_dp}",
                    "authoritative": True}

        relock = int(s.get("auto_relock_seconds", DEFAULT_RELOCK_SECONDS))
        last = self._load_state().get("last_unlock_ts")
        if last is None:
            return {"state": "unknown", "detail": "no unlock observed yet",
                    "authoritative": False}
        elapsed = time.time() - last
        if elapsed < relock:
            return {"state": "UNLOCKED", "detail": f"~{relock - elapsed:.0f}s to auto-relock",
                    "authoritative": False}
        return {"state": "LOCKED", "detail": f"auto-relocked ~{elapsed - relock:.0f}s ago",
                "authoritative": False}

    # -- writes (note: unlock write does not actuate — cloud-gated) ----------
    def set_value(self, dp, value):
        dev = self._dev or self.connect()
        return dev.set_value(dp, value)
