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
        """Perform a remote unlock. Returns True iff the lock echoed dp61=AEkA.

        `on_event(topic, json_str)` is called for each decrypted inbound message.
        """
        client, host, port = self._client()
        state = {"connected": None, "online": False, "unlocked": False}

        def on_connect(c, u, flags, rc, props):
            state["connected"] = int(rc.value) if hasattr(rc, "value") else int(rc)
            if state["connected"] == 0:
                for t in self.in_topics:
                    c.subscribe(t, qos=1)

        def on_message(c, u, msg):
            js = self._decrypt(msg.payload)
            if on_event:
                on_event(msg.topic, js)
            try:
                o = json.loads(js)
            except Exception:
                return
            data = o.get("data", {})
            if o.get("protocol") == 25 and self.cid in (data.get("online") or []):
                state["online"] = True
            if isinstance(data, dict) and data.get("cid") == self.cid and isinstance(data.get("dps"), dict):
                state["online"] = True
                if data["dps"].get(UNLOCK_ECHO[0]) == UNLOCK_ECHO[1]:
                    state["unlocked"] = True

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
            if wake:
                self._publish(client, WAKE_DPS)
                t0 = time.time()
                while time.time() - t0 < online_wait and not state["online"]:
                    time.sleep(0.1)
            for _ in range(attempts):
                self._publish(client, UNLOCK_DPS)
                t0 = time.time()
                while time.time() - t0 < 4 and not state["unlocked"]:
                    time.sleep(0.1)
                if state["unlocked"]:
                    break
            time.sleep(1.5)  # flush trailing reports
            return state["unlocked"]
        finally:
            client.loop_stop()
            client.disconnect()

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
        client.publish(self.out_topic, self._build_frame(dps), qos=1)


class MqttUnlockError(RuntimeError):
    pass
