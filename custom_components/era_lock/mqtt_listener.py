"""Long-lived cloud-MQTT listener for the ERA Protect Lock.

Subscribes to the lock's inbound Tuya MQTT topics and pushes decrypted events to
the entities via HA's dispatcher:
  - unlock events (method DP + credential id -> attribution) -> event entity,
  - lock-state changes (dp61 unlock echo, dp11 anti-lock) -> lock entity.

Runs a paho client in its own thread (paho-managed); callbacks bridge back to
the HA loop with ``call_soon_threadsafe``. On an auth failure (expired session
creds) it re-mints the Tuya session and reconnects. Read-only: it only
subscribes, never publishes, so it can never actuate the lock.

NOTE: the exact inbound value encoding for method DPs is refined from real
captures — every decrypted frame is logged at debug for that purpose.
"""
from __future__ import annotations

import base64
import json
import logging

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN
from .eralock import dpmap
from .eralock.mqtt import UNLOCK_ECHO, CloudMqttLock
from .session import EraSessionManager

_LOGGER = logging.getLogger(__name__)


def signal_event(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_event"


def signal_state(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_state"


def _truthy(v) -> bool:
    return (v is True or (isinstance(v, (int, float)) and bool(v))
            or str(v).lower() in ("true", "1", "on"))


def _dp_value(raw) -> int | None:
    """Decode a method-DP value to its credential id.

    Over MQTT these arrive typed: bools (state dps), ints (method/credential
    ids, e.g. dp62=0 for app-remote), or base64 strings (raw-type dps).
    """
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int.from_bytes(base64.b64decode(raw), "big")
        except Exception:  # noqa: BLE001
            try:
                return int(raw)
            except ValueError:
                return None
    return None


class EraMqttListener:
    """Persistent inbound MQTT listener."""

    def __init__(self, hass: HomeAssistant, mgr: EraSessionManager,
                 combined: dict, entry_id: str) -> None:
        self.hass = hass
        self.mgr = mgr
        self.combined = combined
        self.entry_id = entry_id
        # reuse CloudMqttLock purely for cid / topics / frame decryption
        self._helper = CloudMqttLock(combined, mgr.mqtt_secrets())
        self._roster = dpmap.roster_from_config(combined)
        self._client: mqtt.Client | None = None
        self._closing = False
        self._last_state_ts = 0  # guard against stale DP replays on lock wake
        self._wake_manager = None  # set via set_wake_manager()

    def set_wake_manager(self, wake_manager) -> None:
        self._wake_manager = wake_manager

    # -- lifecycle (start/stop run in an executor thread) --------------------
    def start(self) -> None:
        mq = self.mgr.mqtt_secrets()
        host, port = mq["broker"].replace("ssl://", "").split(":")
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=mq["clientId"],
            clean_session=mq.get("cleanSession", True), protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(mq["username"], mq["password"])
        client.tls_set()
        client.reconnect_delay_set(min_delay=2, max_delay=120)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.connect_async(host, int(port), keepalive=mq.get("keepAlive", 60))
        client.loop_start()
        self._client = client
        _LOGGER.debug("ERA MQTT listener started")

    def stop(self) -> None:
        self._closing = True
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()

    def wake(self) -> None:
        """Publish the synch DP (dp54) so the lock reconnects and reports its
        current DPs, without waiting for the next BLE wake. Not an actuation.
        Use sparingly — waking the BLE lock costs battery.
        """
        from .eralock.mqtt import WAKE_DPS
        if self._client is None:
            return
        self._helper._publish(self._client, WAKE_DPS)
        _LOGGER.debug("ERA MQTT: sent wake/synch (dp54)")

    # -- paho callbacks (paho thread) ----------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        rc = int(reason_code.value) if hasattr(reason_code, "value") else int(reason_code)
        if rc == 0:
            for topic in self._helper.in_topics:
                client.subscribe(topic, qos=1)
            _LOGGER.debug("ERA MQTT connected; subscribed to %s", self._helper.in_topics)
        else:
            _LOGGER.warning("ERA MQTT auth failed (rc=%s); refreshing session", rc)
            self._refresh_and_reconnect()

    def _on_disconnect(self, client, userdata, *args) -> None:
        if not self._closing:
            _LOGGER.debug("ERA MQTT disconnected; paho will retry")

    def _on_message(self, client, userdata, msg) -> None:
        if self._wake_manager is not None:
            self._wake_manager.note_inbound()
        payload = self._helper._decrypt(msg.payload)
        if not payload:
            return
        _LOGGER.debug("ERA MQTT inbound %s: %s", msg.topic, payload)
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        self._handle(obj)

    # -- parsing + dispatch ---------------------------------------------------
    def _handle(self, obj: dict) -> None:
        data = obj.get("data")
        if not isinstance(data, dict):
            return
        dps = data.get("dps")
        if data.get("cid") != self._helper.cid or not isinstance(dps, dict):
            return

        frame_t = obj.get("t", 0)
        tmap = data.get("t") if isinstance(data.get("t"), dict) else {}

        # dp11 = electronic lock state; true=locked (incl. auto-relock), false=
        # unlocked. It's delivered when the BLE lock wakes (with each dp's real
        # event time in data.t), so guard against stale replays by timestamp.
        if "11" in dps:
            ts = tmap.get("11", frame_t)
            if ts >= self._last_state_ts:
                self._last_state_ts = ts
                locked = bool(dps["11"])
                self._dispatch(signal_state, {
                    "locked": locked,
                    "method": "auto-relock (device)" if locked else "device",
                })

        # unlock events (dp61 echo / method dps) -> event + unlocked state
        method: str | None = None
        who: str | None = None
        unlocked = dps.get(UNLOCK_ECHO[0]) == UNLOCK_ECHO[1]  # dp61 "AEkA"
        if unlocked:
            method = "remote"
        for dp, raw in dps.items():
            try:
                dp_id = int(dp)
            except (TypeError, ValueError):
                continue
            if dp_id in dpmap.METHOD_DP_LABELS:
                cred = _dp_value(raw)
                method = dpmap.METHOD_DP_LABELS[dp_id]
                who = dpmap.attribute(dp_id, cred if cred is not None else 0, self._roster)
                self._dispatch(signal_event, {
                    "method": method, "who": who, "credential_id": cred,
                })
                unlocked = True
        if unlocked and frame_t >= self._last_state_ts:
            self._last_state_ts = frame_t
            self._dispatch(signal_state, {"locked": False, "method": method, "who": who})

    def _dispatch(self, signal_fn, payload) -> None:
        self.hass.loop.call_soon_threadsafe(
            async_dispatcher_send, self.hass, signal_fn(self.entry_id), payload)

    # -- recovery -------------------------------------------------------------
    def _refresh_and_reconnect(self) -> None:
        try:
            self.mgr.refresh_tuya_session()
            mq = self.mgr.mqtt_secrets()
            assert self._client is not None
            self._client.username_pw_set(mq["username"], mq["password"])
            self._client.reconnect()
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("ERA MQTT session refresh failed: %s", e)
