"""Cloud-assisted remote unlock + event watch over Tuya MQTT.

Remote unlock rides Tuya MQTT (app → broker → gateway → BLE → lock), NOT the local
LAN (local DP writes don't actuate). This reproduces the app's exact, decrypted
command sequence (see docs/API.md plane C):

  broker  ssl://m1.tuyaeu.com:8883
  topic   smart/mb/out/<hubId>
  frame   b"2.2" + CRC32BE(seq+body)[4] + seq[8] + AES-128-ECB(gatewayLocalKey, PKCS7(json))
  unlock  two steps: {"54":"Aw=="} (wake/sync) then, once the lock reports online,
          {"61":"AQ=="} (remote unlock); lock echoes inbound {"61":"AEkA"} on success.

Signing/encryption uses the gateway localKey (lock_secrets.json). The MQTT connect
credentials (mqtt_secrets.json) are session-derived and ephemeral today; deriving
them from the Tuya login is the remaining durability work (step B) and is the slot
a Home Assistant config entry / login routine will fill.
"""
from __future__ import annotations

import json
import struct
import threading
import time
import zlib

try:
    import paho.mqtt.client as mqtt
except ImportError as e:  # pragma: no cover
    raise ImportError("paho-mqtt is required for cloud unlock: pip install paho-mqtt") from e
try:
    from Crypto.Cipher import AES
except ImportError as e:  # pragma: no cover
    raise ImportError("pycryptodome is required for cloud unlock: pip install pycryptodome") from e

from .secrets import load_lock_secrets, load_mqtt_secrets

WAKE_DPS = {"54": "Aw=="}       # synch_method — wakes/reconnects the BLE lock
UNLOCK_DPS = {"61": "AQ=="}     # remote unlock
UNLOCK_ECHO = ("61", "AEkA")    # inbound dp confirming a successful unlock


class CloudMqttLock:
    """Remote unlock over Tuya MQTT, using the gateway localKey + session creds."""

    def __init__(self, lock_secrets: dict, mqtt_secrets: dict):
        self.ls = lock_secrets
        self.mq = mqtt_secrets
        self.cid = lock_secrets["lock_cid"]
        self.hub = lock_secrets["hub_id"]
        self.local_key = lock_secrets["local_key"].encode()
        self.out_topic = f"smart/mb/out/{self.hub}"
        self.in_topics = [f"smart/mb/in/{self.cid}", f"smart/mb/in/{self.hub}"]

    @classmethod
    def load(cls) -> "CloudMqttLock":
        return cls(load_lock_secrets(), load_mqtt_secrets())

    # -- wire format ----------------------------------------------------------
    def _build_frame(self, dps: dict) -> bytes:
        cmd = {"data": {"cid": self.cid, "ctype": 0, "dps": dps},
               "protocol": 5, "t": int(time.time())}
        pt = json.dumps(cmd, separators=(",", ":")).encode()
        pad = 16 - (len(pt) % 16)
        pt += bytes([pad]) * pad
        body = AES.new(self.local_key, AES.MODE_ECB).encrypt(pt)
        seq = struct.pack(">II", 3, int(time.time() * 1000) & 0xFFFFFFFF)
        crc = struct.pack(">I", zlib.crc32(seq + body) & 0xFFFFFFFF)
        return b"2.2" + crc + seq + body

    def _decrypt(self, payload: bytes):
        try:
            body = payload[15:]
            if len(body) % 16:
                return None
            pt = AES.new(self.local_key, AES.MODE_ECB).decrypt(body)
            return pt[:-pt[-1]].decode("utf-8", "replace")
        except Exception:
            return None

    def _client(self) -> tuple:
        host, port = self.mq["broker"].replace("ssl://", "").split(":")
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.mq["clientId"],
                        clean_session=self.mq.get("cleanSession", True), protocol=mqtt.MQTTv311)
        c.username_pw_set(self.mq["username"], self.mq["password"])
        c.tls_set()
        return c, host, int(port)

    # -- operations -----------------------------------------------------------
    def unlock(self, *, wake: bool = True, online_wait: float = 6.0,
               attempts: int = 3, on_event=None) -> bool:
        """Perform a remote unlock on a fresh, short-lived connection. Returns
        True iff the lock echoed dp61=AEkA.

        Standalone use only (e.g. the CLI): this opens its own MQTT client with
        the session's clientId, so it must NOT run while another client with the
        same clientId (a persistent listener) is connected — the broker allows
        one connection per clientId and the two would kick each other. A caller
        that already holds a connection drives :meth:`drive_unlock` over it.

        `on_event(topic, json_str)` is called for each decrypted inbound message.
        """
        client, host, port = self._client()
        state = {"connected": None}
        tracker = UnlockTracker(self.cid)

        def on_connect(c, u, flags, rc, props):
            state["connected"] = int(rc.value) if hasattr(rc, "value") else int(rc)
            if state["connected"] == 0:
                for t in self.in_topics:
                    c.subscribe(t, qos=1)

        def on_message(c, u, msg):
            js = self._decrypt(msg.payload)
            if on_event:
                on_event(msg.topic, js)
            tracker.feed(js)

        client.on_connect, client.on_message = on_connect, on_message
        client.connect(host, port, keepalive=self.mq.get("keepAlive", 60))
        client.loop_start()
        try:
            t0 = time.time()
            while time.time() - t0 < 8 and state["connected"] is None:
                time.sleep(0.1)
            if state["connected"] != 0:
                raise MqttUnlockError(
                    f"MQTT auth failed (rc={state['connected']}). "
                    "Session creds likely expired — re-capture mqtt_secrets.json.")
            return self.drive_unlock(lambda dps: self._publish(client, dps), tracker,
                                     wake=wake, online_wait=online_wait,
                                     attempts=attempts)
        finally:
            client.loop_stop()
            client.disconnect()

    def drive_unlock(self, publish, tracker: "UnlockTracker", *, wake: bool = True,
                     online_wait: float = 6.0, attempts: int = 3,
                     echo_wait: float = 4.0, settle: float = 1.5) -> bool:
        """The unlock protocol, over any already-connected, subscribed client.

        `publish(dps)` sends one command frame (e.g. ``self._publish(client, dps)``);
        `tracker` must be fed every decrypted inbound frame by the caller's
        message handler (any thread). Sequence: wake (dp54) and wait up to
        `online_wait` for the lock to report; then up to `attempts` x unlock
        (dp61) each waiting `echo_wait` for the dp61=AEkA echo; then `settle`
        (lets a short-lived connection flush trailing reports — a persistent
        connection passes 0). Returns True iff the echo was seen.
        """
        if wake:
            publish(WAKE_DPS)
            tracker.wait("online", online_wait)
        for _ in range(attempts):
            publish(UNLOCK_DPS)
            if tracker.wait("unlocked", echo_wait):
                break
        if settle:
            time.sleep(settle)
        return tracker.unlocked

    def watch(self, seconds: float, on_event) -> None:
        """Passively subscribe and deliver decrypted inbound messages (no publish)."""
        client, host, port = self._client()
        ok = {"c": None}

        def on_connect(c, u, flags, rc, props):
            ok["c"] = int(rc.value) if hasattr(rc, "value") else int(rc)
            if ok["c"] == 0:
                for t in self.in_topics:
                    c.subscribe(t, qos=1)

        def on_message(c, u, msg):
            on_event(msg.topic, self._decrypt(msg.payload))

        client.on_connect, client.on_message = on_connect, on_message
        client.connect(host, port, keepalive=self.mq.get("keepAlive", 60))
        client.loop_start()
        try:
            t0 = time.time()
            while time.time() - t0 < 8 and ok["c"] is None:
                time.sleep(0.1)
            if ok["c"] != 0:
                raise MqttUnlockError(f"MQTT auth failed (rc={ok['c']}).")
            time.sleep(seconds)
        finally:
            client.loop_stop()
            client.disconnect()

    def _publish(self, client, dps: dict):
        return client.publish(self.out_topic, self._build_frame(dps), qos=1)


class UnlockTracker:
    """Unlock progress for one :meth:`CloudMqttLock.drive_unlock` run.

    Thread-safe: :meth:`feed` is called from the MQTT network thread with each
    decrypted inbound frame (it only observes — the caller still processes the
    frame as usual); :meth:`wait` blocks the unlocking thread. Only frames fed
    after construction count, so a fresh tracker per unlock never sees a stale
    echo.
    """

    def __init__(self, cid: str):
        self.cid = cid
        self.online = False     # lock reported (any dps frame / p25 online list)
        self.unlocked = False   # dp61=AEkA echo seen
        self._cond = threading.Condition()

    def feed(self, frame) -> None:
        """Observe one decrypted frame (JSON str, or an already-parsed dict)."""
        if not isinstance(frame, dict):
            try:
                frame = json.loads(frame)
            except Exception:
                return
            if not isinstance(frame, dict):
                return
        data = frame.get("data")
        if not isinstance(data, dict):
            return
        online = unlocked = False
        listed = data.get("online")
        if frame.get("protocol") == 25 and isinstance(listed, list) and self.cid in listed:
            online = True
        if data.get("cid") == self.cid and isinstance(data.get("dps"), dict):
            online = True
            unlocked = data["dps"].get(UNLOCK_ECHO[0]) == UNLOCK_ECHO[1]
        if online or unlocked:
            with self._cond:
                self.online = self.online or online
                self.unlocked = self.unlocked or unlocked
                self._cond.notify_all()

    def wait(self, what: str, timeout: float) -> bool:
        """Block until flag `what` ("online"/"unlocked") is set or `timeout`."""
        with self._cond:
            return self._cond.wait_for(lambda: getattr(self, what), timeout)


class MqttUnlockError(RuntimeError):
    pass
