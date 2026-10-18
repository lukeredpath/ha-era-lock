"""Long-lived cloud-MQTT listener for the ERA Protect Lock (Layer 1 source).

Subscribes to the lock's inbound Tuya MQTT topics, decrypts each frame, and
normalises it into :class:`LockEvent`s published on the ``signal_raw`` dispatcher
signal. It carries NO decision logic — ordering, debounce, attribution and state
inference all live in the controller (Layer 2).

Runs a paho client in its own thread (paho-managed); callbacks bridge back to
the HA loop with ``call_soon_threadsafe``. On an auth failure (expired session
creds) it re-mints the Tuya session and reconnects.

It is the integration's ONLY Tuya MQTT connection. The broker allows one
connection per clientId (and ours is a fixed config constant), so anything that
publishes — the wake/synch (:meth:`EraMqttListener.wake`) and the HA remote
unlock (:meth:`EraMqttListener.unlock`) — rides this client rather than opening
a second one, which would kick this one off (and be kicked back) in a loop.

NOTE: the exact inbound value encoding for method DPs is refined from real
captures — every decrypted frame is logged at debug for that purpose.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .eralock import dpmap
from .eralock.mqtt import UNLOCK_ECHO, CloudMqttLock, MqttUnlockError, UnlockTracker
from .events import LockEvent, signal_raw
from .session import EraSessionManager

_LOGGER = logging.getLogger(__name__)

# A DP whose device event-time is within this of the frame time is a LIVE action;
# older than this it's a buffered history replay (the lock dumps changes on wake).
FRESH_WINDOW = 25

# How long an unlock waits for the listener to be connected before failing.
CONNECT_WAIT_S = 10.0
# A session refresh within this window is reused rather than re-minted (the
# unlock retry and the paho thread's auth-failure path can both ask for one).
REFRESH_REUSE_S = 30.0


def _alarm_value(raw):
    """Decode a dp21 (alarm_lock) enum value to its string.

    Enum DPs may arrive as a plain string or base64-encoded; decode the latter,
    else use the value as-is."""
    if isinstance(raw, str):
        try:
            return base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception:  # noqa: BLE001
            return raw
    return raw


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
        # The lock's Tuya devId (optional config): protocol:1 connectStatus frames
        # name the device by devId, not by the sub-device cid.
        self._lock_dev_id = combined.get("lock_id")
        self._client: mqtt.Client | None = None
        self._closing = False
        self._wake_manager = None  # set via set_wake_manager()
        # Thread-safety: paho callbacks run on paho's network thread; unlock /
        # wake / refresh_session run on HA executor threads.
        self._connected = threading.Event()   # CONNACK rc=0 seen, not since dropped
        self._unlock_lock = threading.Lock()  # one unlock at a time
        self._trackers: set[UnlockTracker] = set()
        self._trackers_lock = threading.Lock()
        self._session_lock = threading.Lock()  # serialises session re-mints
        self._refreshed_at: float | None = None  # monotonic time of last re-mint

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
        self._connected.clear()
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

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def unlock(self, *, connect_wait: float = CONNECT_WAIT_S, **kw) -> bool:
        """Remote unlock over THIS connection (blocking: run in an executor).

        Same protocol as :meth:`CloudMqttLock.unlock` (wake dp54 -> online wait
        -> dp61 x attempts, confirmed by the inbound dp61=AEkA echo) but driven
        over the listener's own client, so no second same-clientId connection
        is opened. Confirmation is observed via an :class:`UnlockTracker` the
        paho thread feeds from ``_on_message``; frames are still dispatched to
        the controller as usual. ``kw`` passes through to ``drive_unlock``.

        Raises :class:`MqttUnlockError` if the listener isn't connected within
        `connect_wait` (or drops mid-unlock). Returns False if unconfirmed.
        """
        kw.setdefault("settle", 0.0)  # persistent connection: nothing to flush
        with self._unlock_lock:
            if self._client is None or self._closing:
                raise MqttUnlockError("ERA MQTT listener is not running")
            if not self._connected.wait(connect_wait):
                raise MqttUnlockError(
                    f"ERA MQTT listener not connected after {connect_wait:.0f}s")
            tracker = UnlockTracker(self._helper.cid)
            infos: list = []
            with self._trackers_lock:
                self._trackers.add(tracker)
            t0 = time.monotonic()
            try:
                ok = self._helper.drive_unlock(
                    lambda dps: self._publish_connected(dps, infos), tracker, **kw)
            finally:
                with self._trackers_lock:
                    self._trackers.discard(tracker)
                self._drop_unsent(infos)
            _LOGGER.debug("ERA MQTT unlock %s in %.1fs",
                          "confirmed" if ok else "NOT confirmed", time.monotonic() - t0)
            return ok

    def _publish_connected(self, dps: dict, infos: list) -> None:
        # Never hand paho a frame while disconnected: a QoS-1 publish made then
        # is queued (rc=NO_CONN) and sent on reconnect — possibly much later.
        # Harmless for dp54, but an unlock must never be delivered late. The
        # check races a drop, so every info is recorded for _drop_unsent.
        if not self._connected.is_set():
            raise MqttUnlockError("ERA MQTT listener disconnected mid-unlock")
        info = self._helper._publish(self._client, dps)
        infos.append(info)
        rc = getattr(info, "rc", 0)
        if rc:
            raise MqttUnlockError(f"ERA MQTT publish failed (rc={rc})")

    def _drop_unsent(self, infos: list) -> None:
        """Best-effort: discard any of this unlock's QoS-1 frames paho still
        holds un-acked, so a later reconnect can't replay them (paho resends
        un-acked QoS>0 messages on reconnect, even with clean_session). Touches
        paho internals (2.x), hence the guards; the common case — every frame
        PUBACKed within ms — never gets here."""
        client = self._client
        def unacked(info) -> bool:
            try:
                return not info.is_published()
            except Exception:  # noqa: BLE001 — raises when rc != 0 (still queued)
                return True

        pending = [i for i in infos if hasattr(i, "mid") and unacked(i)]
        if not pending or client is None:
            return
        try:
            from paho.mqtt.client import mqtt_ms_wait_for_puback
            with client._out_message_mutex:
                for info in pending:
                    msg = client._out_messages.pop(info.mid, None)
                    if (msg is not None and msg.state == mqtt_ms_wait_for_puback
                            and client._inflight_messages > 0):
                        client._inflight_messages -= 1
            _LOGGER.debug("ERA MQTT: dropped %d un-acked unlock frame(s)", len(pending))
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("ERA MQTT: could not drop un-acked unlock frames: %s", e)

    def refresh_session(self) -> None:
        """Re-mint the Tuya session and apply its MQTT creds to THIS client
        (blocking: run in an executor). If the listener is down, paho's own
        reconnect loop then connects with them; no second client is opened."""
        with self._session_lock:
            self._remint()
            self._apply_creds()

    # -- paho callbacks (paho thread) ----------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        rc = int(reason_code.value) if hasattr(reason_code, "value") else int(reason_code)
        if rc == 0:
            for topic in self._helper.in_topics:
                client.subscribe(topic, qos=1)
            self._connected.set()
            _LOGGER.debug("ERA MQTT connected; subscribed to %s", self._helper.in_topics)
        else:
            self._connected.clear()
            _LOGGER.warning("ERA MQTT auth failed (rc=%s); refreshing session", rc)
            self._refresh_and_reconnect()

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected.clear()
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
        self._feed_trackers(obj)
        self._handle(obj)

    def _feed_trackers(self, obj) -> None:
        """Let any in-flight unlock observe this frame (non-consuming)."""
        with self._trackers_lock:
            trackers = list(self._trackers)
        for tracker in trackers:
            try:
                tracker.feed(obj)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("ERA MQTT: unlock tracker feed failed", exc_info=True)

    # -- parsing + dispatch ---------------------------------------------------
    def _handle(self, obj: dict) -> None:
        data = obj.get("data")
        if not isinstance(data, dict):
            return
        proto = obj.get("protocol")
        if proto == 25:
            self._handle_presence(data, obj.get("t"))
            return
        if proto == 1:
            self._handle_connect_status(data, obj.get("t"))
            return
        dps = data.get("dps")
        if data.get("cid") != self._helper.cid or not isinstance(dps, dict):
            return

        frame_t = obj.get("t", 0)
        tmap = data.get("t") if isinstance(data.get("t"), dict) else {}

        # dp11 = electronic lock state (true=locked incl. auto-relock + manual
        # handle-lift, false=unlocked). The lock buffers changes and replays them
        # on wake with each dp's real event time in data.t, so we pass the
        # device-time + a FRESH flag. The controller orders by device-time and
        # treats fresh events as live (side-effects + logged cause); stale ones
        # only sync the state.
        if "11" in dps:
            dp_ts = tmap.get("11")
            dt = dp_ts if dp_ts is not None else frame_t
            fresh = dp_ts is None or (frame_t - dp_ts) <= FRESH_WINDOW
            self._emit(LockEvent(
                type="state_report", locked=bool(dps["11"]), dt=dt, fresh=fresh,
                source="device"))

        # unlock events (dp61 echo / method dps)
        method: str | None = None
        who: str | None = None
        cred_id: int | None = None
        unlock_dt = frame_t
        unlocked = dps.get(UNLOCK_ECHO[0]) == UNLOCK_ECHO[1]  # dp61 "AEkA"
        if unlocked:
            method = "remote"
        for dp, raw in dps.items():
            try:
                dp_id = int(dp)
            except (TypeError, ValueError):
                continue
            if dp_id in dpmap.METHOD_DP_LABELS:
                cred_id = _dp_value(raw)
                method = dpmap.METHOD_DP_LABELS[dp_id]
                # ``who`` is the roster NAME only (None when unrostered); the
                # raw "method #id" fallback is the controller's to render.
                who = (self._roster.get((dp_id, cred_id))
                       if cred_id is not None else None)
                unlock_dt = tmap.get(str(dp_id), frame_t)
                unlocked = True
        if unlocked:
            fresh = (frame_t - unlock_dt) <= FRESH_WINDOW
            self._emit(LockEvent(
                type="unlock", dt=unlock_dt, fresh=fresh, source="device",
                method=method, who=who, cred=cred_id))

        # dp21 = alarm_lock enum (wrong_finger + other alarms); dp22 = hijack.
        if "21" in dps:
            val = _alarm_value(dps["21"])
            if val not in (None, "", 0):
                dp_ts = tmap.get("21")
                dt = dp_ts if dp_ts is not None else frame_t
                fresh = dp_ts is None or (frame_t - dp_ts) <= FRESH_WINDOW
                if val == "wrong_finger":
                    self._emit(LockEvent(
                        type="failed_unlock", method="fingerprint", dt=dt,
                        fresh=fresh, source="device"))
                else:
                    self._emit(LockEvent(
                        type="tamper", subtype=str(val), dt=dt, fresh=fresh,
                        source="device"))
        if "22" in dps and _truthy(dps["22"]):
            dp_ts = tmap.get("22")
            dt = dp_ts if dp_ts is not None else frame_t
            fresh = dp_ts is None or (frame_t - dp_ts) <= FRESH_WINDOW
            self._emit(LockEvent(
                type="tamper", subtype="hijack", dt=dt, fresh=fresh,
                source="device"))

    # -- link state (hub's view of the BLE lock) -------------------------------
    def _handle_presence(self, data: dict, frame_t) -> None:
        """protocol:25 — the hub's sub-device presence list:
        ``{"online": [cid..], "offline": [cid..], "nearby": [cid..]}``. "nearby"
        is the lock asleep within BLE range (normal; it's listed alongside
        "online"), so only an ``offline`` listing counts as offline. A frame that
        doesn't mention our cid says nothing about it."""
        cid = self._helper.cid

        def has(key) -> bool:
            v = data.get(key)
            return isinstance(v, list) and cid in v

        if has("online") or has("nearby"):
            online = True
        elif has("offline"):
            online = False
        else:
            return
        self._emit_link(online, frame_t, "presence")

    def _handle_connect_status(self, data: dict, frame_t) -> None:
        """protocol:1 — a device's connection status, keyed by Tuya devId:
        ``connectStatus`` 1 = connected/awake, 2 = asleep but nearby (normal),
        0 = offline; ``online`` mirrors it. Only matched when the lock's devId
        (``lock_id``) is configured; protocol:25 covers it otherwise."""
        if not self._lock_dev_id or data.get("devId") != self._lock_dev_id:
            return
        status = data.get("connectStatus")
        flag = data.get("online")
        if status == 0 or flag is False:
            online = False
        elif status in (1, 2) or flag is True:
            online = True
        else:
            return
        self._emit_link(online, frame_t, "connectStatus")

    def _emit_link(self, online: bool, frame_t, via: str) -> None:
        self._emit(LockEvent(
            type="connectivity", value=online, subtype=via,
            dt=frame_t if isinstance(frame_t, (int, float)) else time.time(),
            source="hub"))

    def _emit(self, event: LockEvent) -> None:
        self.hass.loop.call_soon_threadsafe(
            async_dispatcher_send, self.hass, signal_raw(self.entry_id), event)

    # -- recovery -------------------------------------------------------------
    def _remint(self) -> None:
        """Re-mint the Tuya session unless one was just minted (caller holds
        ``_session_lock``)."""
        now = time.monotonic()
        if self._refreshed_at is not None and now - self._refreshed_at < REFRESH_REUSE_S:
            return
        self.mgr.refresh_tuya_session()
        self._refreshed_at = time.monotonic()

    def _apply_creds(self) -> None:
        mq = self.mgr.mqtt_secrets()
        assert self._client is not None
        self._client.username_pw_set(mq["username"], mq["password"])

    def _refresh_and_reconnect(self) -> None:
        # paho thread (from _on_connect on an auth failure)
        try:
            with self._session_lock:
                self._remint()
                self._apply_creds()
            assert self._client is not None
            self._client.reconnect()
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("ERA MQTT session refresh failed: %s", e)
