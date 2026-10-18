"""v0.1.3: auth lifecycle (persist re-mints, reauth once on a genuine rejection,
no hot retry), setup outcomes (connected / ConfigEntryAuthFailed /
ConfigEntryNotReady), shutdown hygiene, the update listener's reload rules, the
battery from MQTT dp8, wake hygiene, the post-close wake series, the door hold's
cause-before-effect ordering, and diagnostics redaction.

Runs the REAL __init__ / coordinator / session manager / listener / wake
manager / controller / sensor / diagnostics on the shared offline harness
(test_controller_harness.py) plus a few extra stubs (no network, no HA).

Run (from the repo root):  python3 -I -B tests/offline/test_lifecycle.py
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
import threading
import time as _rtime
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops it
import test_controller_harness as H  # noqa: E402
from test_controller_harness import (  # noqa: E402
    CLOCK, FAKE_CLIENTS, LockEvent, activity_rows, check, controller_mod, door,
    make, raw, state_changes, timeline,
)

logging.getLogger("era_lock").setLevel(logging.CRITICAL)
# The fake config-entries manager collects update-listener coroutines; tests
# that don't care about them never await them.
import warnings  # noqa: E402
warnings.filterwarnings("ignore", message="coroutine .*_async_update_listener")

# ------------------------------------------------------------ extra stubs --
era_stub = sys.modules["era_lock.eralock.era"]
AUTH = {"refresh": None, "creds": None, "login": None}  # behaviour knobs


class Toks:
    def __init__(self, n):
        self.id_token, self.refresh_token, self.expires_in = f"id{n}", None, 86400


def _cognito_refresh(refresh_token, *, client_id, region):
    AUTH["refresh_calls"] = AUTH.get("refresh_calls", 0) + 1
    if AUTH["refresh"] is not None:
        raise AUTH["refresh"]
    t = Toks(AUTH["refresh_calls"])
    t.refresh_token = refresh_token  # Cognito returns no new one
    return t


def _fetch_tuya_creds(id_token, email, **kw):
    if AUTH["creds"] is not None:
        raise AUTH["creds"]
    return {"tuid": "TUID", "tpassword": "TPW"}


era_stub.cognito_refresh = _cognito_refresh
era_stub.cognito_login = lambda *a, **k: None
era_stub.fetch_tuya_creds = _fetch_tuya_creds

esession = importlib.import_module("era_lock.eralock.session")  # real
MINTS = {"n": 0}


class FakeTuyaLogin:
    def __init__(self, *, sign_secrets=None, tuid=None):
        pass

    def login_session(self, password):
        if AUTH["login"] is not None:
            raise AUTH["login"]
        MINTS["n"] += 1
        n = MINTS["n"]
        return esession.Session(uid="U", partner="P", ch_key="CH",
                                token=f"tok{n}", ecode=f"ec{n}")


H.mod("era_lock.eralock.login", TuyaLogin=FakeTuyaLogin)


class FakeLocalLock:
    dps_value = {"8": 77, "11": True}

    def __init__(self, combined):
        self._dev = None

    def dps(self):
        return dict(self.dps_value)


H.mod("era_lock.eralock.local", LocalLock=FakeLocalLock)


class DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, *, config_entry=None, name=None,
                 update_interval=None):
        self.hass, self.config_entry, self.name = hass, config_entry, name
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = True
        self.listener_calls = 0

    async def async_refresh(self):
        self.data = await self._async_update_data()

    async def async_request_refresh(self):
        await self.async_refresh()

    def async_update_listeners(self):
        self.listener_calls += 1


class UpdateFailed(Exception):
    pass


uc = sys.modules["homeassistant.helpers.update_coordinator"]
uc.DataUpdateCoordinator, uc.UpdateFailed = DataUpdateCoordinator, UpdateFailed
hconst = sys.modules["homeassistant.const"]
hconst.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
hconst.PERCENTAGE = "%"


class SensorEntity:
    hass = None


H.mod("homeassistant.components.sensor", SensorEntity=SensorEntity,
      SensorDeviceClass=types.SimpleNamespace(BATTERY="battery"))

REDACTED = "**REDACTED**"


def async_redact_data(data, to_redact):  # mirrors HA's helper
    if isinstance(data, list):
        return [async_redact_data(v, to_redact) for v in data]
    if not isinstance(data, dict):
        return data
    out = {}
    for k, v in data.items():
        if k in to_redact:
            out[k] = REDACTED
        elif isinstance(v, (dict, list)):
            out[k] = async_redact_data(v, to_redact)
        else:
            out[k] = v
    return out


H.mod("homeassistant.components.diagnostics", async_redact_data=async_redact_data)


async def _get_integration(hass, domain):
    return types.SimpleNamespace(version="0.1.3-test")


H.mod("homeassistant.loader", async_get_integration=_get_integration)

# The real session manager + coordinator (the harness stubs them).
for name in ("era_lock.session", "era_lock.coordinator"):
    sys.modules.pop(name, None)
session_mod = importlib.import_module("era_lock.session")
coord_mod = importlib.import_module("era_lock.coordinator")
# The real package __init__ (async_setup_entry & co), executed into the package.
pkg = sys.modules["era_lock"]
pkg.__package__ = "era_lock"
pkg.__file__ = os.path.join(H.PKG_DIR, "__init__.py")
exec(compile(open(pkg.__file__).read(), pkg.__file__, "exec"), pkg.__dict__)
init = pkg
sensor_mod = importlib.import_module("era_lock.sensor")
diag_mod = importlib.import_module("era_lock.diagnostics")
const = importlib.import_module("era_lock.const")
listener_mod = H.listener_mod
listener_mod.STOP_JOIN_S = 0.3

# ----------------------------------------------------------- fake HA bits --
SECRETS = {"hmac_key": "HMACSECRET123", "local_key": "LOCALKEYSECRET",
           "hub_id": "HUBID-SECRET", "lock_cid": "CID1", "chKey": "CHKEYSECRET",
           "mqtt_client_id": "MQTTCLIENTSECRET", "hub_ip": "192.0.2.10",
           "lock_id": "LOCKID-SECRET"}
SERVICE = {"cognito_client_id": "COGNITOCLIENT", "cognito_region": "eu-west-1",
           "appsync_url": "https://appsync.invalid/graphql", "clientId": "APPKEYSECRET"}
SESSION0 = {"uid": "U", "partner": "P", "ch_key": "CH", "token": "tok0",
            "ecode": "ec0"}


class FakeEntry:
    def __init__(self, options=None):
        self.entry_id = "E1"
        self.unique_id = "HUB"
        self.domain = "era_lock"
        self.title = "ERA Protect Lock"
        self.version = 1
        self.state = "loaded"
        self.data = {"era": {"refresh_token": "RT-SECRET", "email": "me@example.invalid"},
                     "session": dict(SESSION0), "service_config": dict(SERVICE),
                     "secrets": dict(SECRETS)}
        self.options = options if options is not None else {}
        self.update_listeners = []
        self.on_unload = []
        self.reauths = 0
        self.bg_tasks = []

    def async_on_unload(self, f):
        self.on_unload.append(f)

    def add_update_listener(self, f):
        self.update_listeners.append(f)
        return lambda: self.update_listeners.remove(f)

    def async_create_background_task(self, hass, coro, name):
        self.bg_tasks.append(name)
        return asyncio.ensure_future(coro)

    def async_start_reauth(self, hass):
        self.reauths += 1


class FakeConfigEntries:
    def __init__(self, hass):
        self.hass = hass
        self.forwarded = []
        self.reloads = []

    async def async_forward_entry_setups(self, entry, platforms):
        self.forwarded.append(list(platforms))

    def async_update_entry(self, entry, data=None, options=None):
        changed = False
        if data is not None and data != entry.data:
            entry.data, changed = data, True
        if options is not None and options != entry.options:
            entry.options, changed = options, True
        if changed:
            for f in list(entry.update_listeners):
                self.hass.pending.append(f(self.hass, entry))
        return changed

    async def async_reload(self, entry_id):
        self.reloads.append(entry_id)

    async def async_unload_platforms(self, entry, platforms):
        return True


def make_hass():
    hass = types.SimpleNamespace()
    hass.pending = []
    hass.loop = types.SimpleNamespace(call_soon_threadsafe=lambda f, *a: f(*a))

    class _Done:  # HA schedules the job whether or not it's awaited
        def __init__(self, v):
            self.v = v

        def __await__(self):
            return self.v
            yield  # noqa: unreachable — makes this a generator

    hass.async_add_executor_job = lambda f, *a: _Done(f(*a))
    hass.config_entries = FakeConfigEntries(hass)
    hass.stop_listeners = []
    hass.bus = types.SimpleNamespace(
        async_listen_once=lambda et, cb: hass.stop_listeners.append((et, cb))
        or (lambda: None),
        async_fire=lambda *a, **k: None)
    hass.states = types.SimpleNamespace(get=lambda eid: None)
    hass.log = []
    return hass


def connack_hook(*codes):
    """FakeClient.loop_start behaviour: deliver these CONNACK codes in turn."""
    def hook(client):
        for rc in codes:
            client.on_connect(client, None, {}, rc, None)
    return hook


def reset(codes=(0,)):
    FAKE_CLIENTS.clear()
    MINTS["n"] = 0
    AUTH.update(refresh=None, creds=None, login=None, refresh_calls=0)
    H.FakeClient.loop_start = connack_hook(*codes)
    H.DISPATCH.clear()
    CLOCK.timers.clear()
    H.STORE_DISK.clear()
    H.STORE_PENDING.clear()


async def drain(hass):
    while hass.pending:
        await hass.pending.pop(0)
    await asyncio.sleep(0)


def setup(entry=None, codes=(0,), timeout=None, auth=None, stored=None):
    reset(codes)
    AUTH.update(auth or {})
    if stored is not None:  # the lock state saved before this "restart"
        H.STORE_DISK["era_lock.E1"] = dict(stored)
    hass = make_hass()
    entry = entry or FakeEntry()
    if timeout is not None:
        init.STARTUP_CONNECT_TIMEOUT_S = timeout
    out = {}

    async def go():
        try:
            out["ok"] = await init.async_setup_entry(hass, entry)
        except Exception as e:  # noqa: BLE001
            out["err"] = e
        await drain(hass)
    asyncio.run(go())
    init.STARTUP_CONNECT_TIMEOUT_S = const.STARTUP_CONNECT_TIMEOUT_S
    return hass, entry, out


def dps_sent(client):
    return [f["data"]["dps"] for _, f in client.published]


# ------------------------------------------------------------------ tests --
def t_setup_connected():
    hass, entry, out = setup()
    c = entry.runtime_data
    client = FAKE_CLIENTS[-1]
    check("setup: connected -> True, platforms forwarded AFTER the connect",
          out.get("ok") is True and hass.config_entries.forwarded
          and c.listener.connected, out)
    check("setup: one startup sync (dp54) published once connected",
          dps_sent(client) == [{"54": "Aw=="}], dps_sent(client))
    check("setup: first LAN poll runs as a background task (doesn't block setup)",
          entry.bg_tasks == ["era_lock first LAN poll"], entry.bg_tasks)
    check("setup: HA-stop listener + update listener registered",
          [et for et, _ in hass.stop_listeners] == ["homeassistant_stop"]
          and len(entry.update_listeners) == 1)
    check("setup: no session re-mint for a good stored session",
          MINTS["n"] == 0 and entry.data["session"] == SESSION0)
    check("setup: DataUpdateCoordinator got config_entry=entry",
          c.config_entry is entry)


def t_setup_stale_session_persisted():
    hass, entry, out = setup(codes=(5, 0))  # CONNACK refused, then OK
    c = entry.runtime_data
    client = FAKE_CLIENTS[-1]
    check("persist: stale stored session -> re-minted, reconnected, setup OK",
          out.get("ok") is True and MINTS["n"] == 1 and client.reconnects == 1
          and client.creds_history[-1][0].find("tok1") >= 0, (out, MINTS))
    check("persist: re-minted session written to the entry's data",
          entry.data["session"]["token"] == "tok1"
          and entry.data["era"]["refresh_token"] == "RT-SECRET"
          and entry.data["era"]["email"] == "me@example.invalid", entry.data)
    check("persist: ...recorded as our own (update listener will not reload)",
          c.persisted_creds == coord_mod.entry_credentials(entry.data))
    # A later runtime re-mint with a ROTATED refresh token: persisted, no reload.
    CLOCK.advance(1)
    c.listener._refreshed_at = None
    orig = era_stub.cognito_refresh

    def rotating(rt, **kw):
        t = orig(rt, **kw)
        t.refresh_token = "RT-ROTATED"
        return t
    era_stub.cognito_refresh = rotating
    session_mod.cognito_refresh = rotating
    c.mgr._id_token = None
    try:
        c.listener._on_connect(client, None, {}, 5, None)
    finally:
        era_stub.cognito_refresh = orig
        session_mod.cognito_refresh = orig
    asyncio.run(drain(hass))
    check("persist: runtime re-mint persists the new session + rotated refresh token",
          entry.data["session"]["token"] == "tok2"
          and entry.data["era"]["refresh_token"] == "RT-ROTATED", entry.data)
    check("persist: data-only self-persist does NOT reload",
          not hass.config_entries.reloads, hass.config_entries.reloads)


def t_setup_auth_failed():
    hass, entry, out = setup(codes=(5,), auth={"refresh": era_stub.EraAuthError(
        "ERA session expired (NotAuthorizedException)")})
    AUTH["refresh"] = None
    client = FAKE_CLIENTS[-1]
    check("setup: account rejects the re-mint -> ConfigEntryAuthFailed",
          isinstance(out.get("err"), H.ConfigEntryAuthFailed), out)
    check("setup: ...listener stopped, no reconnect attempted, entities not created",
          entry.runtime_data.listener._closing and client.reconnects == 0
          and not hass.config_entries.forwarded)
    check("setup: ...reauth left to HA (ConfigEntryAuthFailed starts it): none by us",
          entry.reauths == 0, entry.reauths)


def t_setup_transport_not_ready():
    hass, entry, out = setup(codes=(5,), timeout=0.2, auth={
        "refresh": era_stub.EraConnectionError("Cognito request failed: Timeout")})
    AUTH["refresh"] = None
    check("setup: ERA unreachable during the re-mint -> ConfigEntryNotReady (not reauth)",
          isinstance(out.get("err"), H.ConfigEntryNotReady) and entry.reauths == 0
          and not entry.runtime_data.listener.auth_failed, out)
    hass, entry, out = setup(codes=(), timeout=0.2)  # broker never answers
    check("setup: no CONNACK in time -> ConfigEntryNotReady, listener stopped",
          isinstance(out.get("err"), H.ConfigEntryNotReady)
          and entry.runtime_data.listener._closing
          and not hass.config_entries.forwarded, out)


def t_runtime_auth_failure():
    hass, entry, out = setup()
    c = entry.runtime_data
    lst = c.listener
    client = FAKE_CLIENTS[-1]
    states = []
    H.async_dispatcher_connect(hass, H.events.signal_state("E1"), states.append)
    disconnects = []
    client.disconnect = lambda: disconnects.append(CLOCK.now)
    lst._on_disconnect(client, None, None, 0, None)
    AUTH["refresh"] = era_stub.EraAuthError("refresh token expired")
    c.mgr._id_token = None
    n0 = AUTH["refresh_calls"]
    lst._on_connect(client, None, {}, 5, None)
    check("auth: genuine rejection -> reauth started exactly once",
          entry.reauths == 1, entry.reauths)
    check("auth: ...listener disconnects (stops paho's retry loop), no reconnect",
          disconnects and client.reconnects == 0 and lst.auth_failed)
    check("auth: ...lock entity unavailable",
          states and states[-1]["available"] is False
          and c.controller.available is False, states[-1:])
    lst._on_connect(client, None, {}, 5, None)
    lst._on_connect(client, None, {}, 5, None)
    check("auth: further refused CONNACKs -> no re-mint attempts (no hot retry), "
          "no second reauth",
          AUTH["refresh_calls"] == n0 + 1 and entry.reauths == 1,
          (AUTH["refresh_calls"], entry.reauths))
    client._thread = None
    n_clients = len(FAKE_CLIENTS)
    lst._async_watchdog()
    check("auth: watchdog never restarts a listener latched on auth failure",
          len(FAKE_CLIENTS) == n_clients)
    n_pub = len(client.published)
    lst.wake()
    check("auth: wake is a no-op", len(client.published) == n_pub)
    try:
        c.controller._unlock()
        err = None
    except H.HomeAssistantError as e:
        err = str(e)
    check("auth: unlock -> clean HomeAssistantError naming re-authentication",
          err and "re-authenticate" in err, err)
    AUTH["refresh"] = None


def t_runtime_transport_failure():
    hass, entry, out = setup()
    c = entry.runtime_data
    lst = c.listener
    client = FAKE_CLIENTS[-1]
    AUTH["creds"] = era_stub.EraConnectionError("AppSync unavailable (HTTP 503)")
    c.mgr._id_token = None
    lst._on_connect(client, None, {}, 5, None)
    check("transport: re-mint transport failure -> NOT latched, no reauth, "
          "lock still available",
          not lst.auth_failed and entry.reauths == 0 and c.controller.available)
    AUTH["creds"] = None
    lst._on_connect(client, None, {}, 5, None)  # paho's backoff retry
    check("transport: the next refused CONNACK re-mints and reconnects",
          MINTS["n"] == 1 and client.reconnects == 1, (MINTS, client.reconnects))
    # AppSync refusing even a freshly refreshed idToken: a new password can't
    # fix that -> retried with backoff, not a reauth.
    lst._refreshed_at = None
    n0 = AUTH["refresh_calls"]
    AUTH["creds"] = era_stub.EraAuthError("AppSync DeviceUser error: [...]")
    lst._on_connect(client, None, {}, 5, None)
    AUTH["creds"] = None
    check("transport: AppSync rejection -> one forced Cognito refresh + retry, then "
          "treated as transient (no latch, no reauth)",
          AUTH["refresh_calls"] >= n0 + 1 and not lst.auth_failed
          and entry.reauths == 0, (AUTH["refresh_calls"] - n0, lst.auth_failed))
    try:
        AUTH["creds"] = era_stub.EraConnectionError("down")
        lst._refreshed_at = None
        lst._connected.clear()
        c.controller.coordinator.listener = types.SimpleNamespace(
            unlock=lambda: (_ for _ in ()).throw(H.MqttUnlockError("not connected")),
            refresh_session=lst.refresh_session, auth_failed=False)
        c.controller._unlock()
        err = None
    except H.HomeAssistantError as e:
        err = str(e)
    finally:
        AUTH["creds"] = None
    check("transport: unlock whose session refresh can't reach ERA -> clean "
          "HomeAssistantError (was a raw exception)",
          err and err.startswith("Unlock failed: could not refresh"), err)


def t_update_listener_rules():
    hass, entry, out = setup(entry=FakeEntry(options={"credentials": []}))
    c = entry.runtime_data

    async def go():
        hass.config_entries.async_update_entry(
            entry, options={"credentials": [{"method": "card", "id": 3, "label": ""}]})
        await drain(hass)
        r1 = list(hass.config_entries.reloads)
        c.async_persist_credentials({"refresh_token": "RT-SECRET",
                                     "session": {**SESSION0, "token": "tokX"}})
        await drain(hass)
        r2 = list(hass.config_entries.reloads)
        # the reauth flow writes NEW credentials (async_update_and_abort)
        hass.config_entries.async_update_entry(entry, data={
            **entry.data, "era": {"refresh_token": "RT-NEW", "email": "me@example.invalid"},
            "session": {**SESSION0, "token": "tokR"}})
        await drain(hass)
        r3 = list(hass.config_entries.reloads)
        hass.config_entries.reloads.clear()
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "auto_relock_seconds": 90})
        await drain(hass)
        r4 = list(hass.config_entries.reloads)
        return r1, r2, r3, r4
    r1, r2, r3, r4 = asyncio.run(go())
    check("update listener: credentials-only options change -> no reload", r1 == [], r1)
    check("update listener: our own re-mint persisted -> no reload", r2 == [], r2)
    check("update listener: reauth's new credentials -> reload", r3 == ["E1"], r3)
    check("update listener: a reload-relevant option -> reload", r4 == ["E1"], r4)


def t_shutdown():
    hass, entry, out = setup()
    c = entry.runtime_data
    client = FAKE_CLIENTS[-1]
    order = []
    client.disconnect = lambda: order.append("disconnect")
    client.loop_stop = lambda: order.append("loop_stop")
    et, cb = hass.stop_listeners[0]
    asyncio.run(cb(None))
    check("stop: HA stop event closes the MQTT session: disconnect BEFORE loop_stop",
          order == ["disconnect", "loop_stop"] and c.listener._closing and c.closing,
          order)
    c.async_persist_credentials({"refresh_token": "x", "session": {}})
    check("stop: no entry writes after closing", entry.data["session"] == SESSION0)
    # A re-mint in flight on paho's (real) network thread: stop() is bounded,
    # and the late re-mint result is discarded (no creds applied, no reconnect).
    hass, entry, out = setup()
    lst = entry.runtime_data.listener
    client = FAKE_CLIENTS[-1]
    gate = threading.Event()
    real_refresh = lst.mgr.refresh_tuya_session
    lst.mgr.refresh_tuya_session = lambda: (gate.wait(5), real_refresh())
    lst._refreshed_at = None
    th = threading.Thread(target=lambda: lst._on_connect(client, None, {}, 5, None),
                          daemon=True)
    client._thread = th
    th.start()
    _rtime.sleep(0.05)
    n_creds = len(client.creds_history)
    t0 = _rtime.monotonic()
    lst.stop()
    took = _rtime.monotonic() - t0
    gate.set()
    th.join(2)
    check("stop: bounded while a re-mint is in flight on paho's thread",
          took < 1.0, took)
    check("stop: ...the late re-mint applies nothing and doesn't reconnect",
          len(client.creds_history) == n_creds and client.reconnects == 0,
          (client.creds_history, client.reconnects))


def t_wake_hygiene():
    hass, entry, out = setup()
    c = entry.runtime_data
    lst, client = c.listener, FAKE_CLIENTS[-1]
    lst._on_disconnect(client, None, None, 0, None)
    n = len(client.published)
    lst.wake()
    check("wake: no-op (nothing queued for later) while disconnected",
          len(client.published) == n and lst.stats["wakes_skipped"] >= 1)
    lst._on_connect(client, None, {}, 0, None)
    wm = c.wake
    t_last = wm._last_inbound
    CLOCK.advance(1)
    client.deliver({"protocol": 25, "t": 1, "data": {"online": ["CID1"]}})
    client.deliver({"protocol": 4, "t": 1, "data": {"cid": "OTHER", "dps": {"1": 1}}})
    check("note_inbound: hub presence / other cids don't count as lock activity",
          wm._last_inbound == t_last)
    client.deliver({"protocol": 4, "t": 1, "data": {"cid": "CID1", "dps": {"8": 64}}})
    check("note_inbound: a DP frame from our lock does",
          wm._last_inbound == CLOCK.now)
    check("battery: MQTT dp8 -> coordinator battery + entities notified",
          c.battery == 64 and c.battery_source == "mqtt" and c.listener_calls >= 1,
          (c.battery, c.battery_source))
    # Sync button -> wake manager (forced, coalesced, counted)
    CLOCK.advance(H.wake_mod.FORCED_WAKE_COALESCE_S + 1)  # past the startup sync
    btn = H.button_mod.EraSyncButton(c, entry)
    btn.hass = hass
    n = len(client.published)
    asyncio.run(btn.async_press())
    asyncio.run(btn.async_press())  # 0s later: merged
    check("sync button: goes through the wake manager (one wake, second merged)",
          len(client.published) == n + 1 and wm.stats["merged"] >= 1,
          (len(client.published) - n, wm.stats))
    CLOCK.advance(H.wake_mod.FORCED_WAKE_COALESCE_S + 1)
    asyncio.run(btn.async_press())
    check("sync button: a press after the window bypasses the rate limit",
          len(client.published) == n + 2)


def t_heartbeat():
    check("heartbeat default: 1h with a door contact, 15min without, explicit wins",
          const.wake_heartbeat({"door_contact_entity_id": "binary_sensor.d"}) == 3600
          and const.wake_heartbeat({}) == 900
          and const.wake_heartbeat({"door_contact_entity_id": "x",
                                    "wake_heartbeat_seconds": 600}) == 600
          and const.wake_heartbeat({"wake_heartbeat_seconds": 0}) == 0)
    hass, entry, out = setup(entry=FakeEntry(options={
        "door_contact_entity_id": "binary_sensor.door"}))
    check("heartbeat: setup uses the door-aware default",
          entry.runtime_data.wake.heartbeat_s == 3600)
    wm, lst = H.make_wake()
    wm._heartbeat_s = 900
    t0 = CLOCK.now
    for _ in range(40):  # 40 min of 60s ticks, the lock never answers
        CLOCK.advance(60)
        wm._check_idle(None)
    got = [round((w - t0) / 60) for w in lst.wakes]
    check("heartbeat: a silent lock is woken once per interval, not every tick",
          got == [15, 30], got)


def t_battery_sensor():
    entry = FakeEntry()
    entry.data["secrets"] = {k: v for k, v in SECRETS.items() if k != "hub_ip"}
    hass, entry, out = setup(entry=entry)
    c = entry.runtime_data
    sen = sensor_mod.EraBatterySensor(c, entry)
    check("battery sensor: no hub_ip -> no LAN data, unavailable until dp8",
          not c.has_local and sen.available is False and sen.native_value is None)
    H.raw(c.controller, type="battery", value=55, dt=CLOCK.now)
    check("battery sensor: sourced from MQTT dp8 (no hub_ip needed)",
          sen.available is True and sen.native_value == 55)
    hass, entry, out = setup()
    c = entry.runtime_data
    asyncio.run(c.async_refresh())
    check("battery sensor: LAN poll still feeds it (fallback)",
          c.battery == 77 and c.battery_source == "lan")
    check("LAN poll interval lengthened (6h fallback)",
          c.update_interval.total_seconds() == 6 * 3600)


def t_diagnostics_redaction():
    hass, entry, out = setup(codes=(5, 0))  # also exercises a re-minted session
    entry.options = {"credentials": [{"method": "fingerprint", "id": 1,
                                      "label": "thumb", "user_id": "HAUSERSECRET",
                                      "user_name": "Luke"}],
                     "door_contact_entity_id": "binary_sensor.door"}
    c = entry.runtime_data
    c.listener._latch_auth_failure(era_stub.EraAuthError("expired"))
    diag = asyncio.run(diag_mod.async_get_config_entry_diagnostics(hass, entry))
    blob = json.dumps(diag, default=str)
    secrets = [*SECRETS.values(), "RT-SECRET", "me@example.invalid", "tok0", "tok1",
               "ec1", "APPKEYSECRET", "COGNITOCLIENT", "HAUSERSECRET",
               entry.data["session"]["token"]]
    leaked = [s for s in secrets if s in blob]
    check("diagnostics: every secret / identifier redacted", not leaked, leaked)
    check("diagnostics: lock_cid redacted too",
          diag["entry"]["data"]["secrets"]["lock_cid"] == REDACTED
          and diag["entry"]["data"]["session"] == REDACTED
          and diag["entry"]["data"]["era"]["refresh_token"] == REDACTED
          and diag["entry"]["data"]["era"]["email"] == REDACTED, diag["entry"]["data"])
    rt = diag.get("runtime", {})
    check("diagnostics: useful runtime state present",
          rt.get("listener", {}).get("auth_failed") is True
          and "connected" in rt["listener"] and "state" in rt.get("controller", {})
          and "heartbeat_s" in rt.get("wake", {}) and "battery" in rt
          and diag.get("integration_version") == "0.1.3-test", rt)


# ---------------------------------------- door hold + close series (real wake) --
def make_real(hass_ctl=None):
    """Controller with the REAL wake manager over a fake listener."""
    hass, ctl = make()
    lst = H.FakeListener()
    wm = H.wake_mod.EraWakeManager(hass, lst, heartbeat_s=0)
    ctl.coordinator.wake = wm
    return hass, ctl, wm, lst


def t_close_series():
    hass, ctl, wm, lst = make_real()
    ctl._state = ctl._logged_state = "unlocked"
    hass._door_state = types.SimpleNamespace(state="on")
    t0 = CLOCK.now
    door(hass, ctl, "off")
    CLOCK.advance(40)
    got = [round(w - t0, 1) for w in lst.wakes]
    check("close series: close while unlocked -> close wake + 10/20/30 (no lock)",
          got == [0.0, 10.0, 20.0, 30.0], got)

    hass, ctl, wm, lst = make_real()
    ctl._state = ctl._logged_state = "unlocked"
    hass._door_state = types.SimpleNamespace(state="on")
    t0 = CLOCK.now
    door(hass, ctl, "off")
    CLOCK.advance(12)
    raw(ctl, type="state_report", locked=True, dt=CLOCK.now)  # handle lifted
    CLOCK.advance(40)
    got = [round(w - t0, 1) for w in lst.wakes]
    check("close series: a lock at +12s (settles +14s) cancels the +20/+30 wakes",
          got == [0.0, 10.0] and ctl._state == "locked", got)

    hass, ctl, wm, lst = make_real()
    ctl._state = ctl._logged_state = "unlocked"
    hass._door_state = types.SimpleNamespace(state="on")
    t0 = CLOCK.now
    door(hass, ctl, "off")
    CLOCK.advance(15)
    door(hass, ctl, "on")       # reopened: series cancelled
    CLOCK.advance(10)
    door(hass, ctl, "off")      # +25: new series from here
    CLOCK.advance(40)
    got = [round(w - t0, 1) for w in lst.wakes]
    check("close series: reopen cancels it; the next close restarts it "
          "(door wakes rate-limited)",
          got == [0.0, 10.0, 35.0, 45.0, 55.0], got)

    hass, ctl, wm, lst = make_real()
    ctl._state = ctl._logged_state = "locked"
    hass._door_state = types.SimpleNamespace(state="on")
    door(hass, ctl, "off")
    CLOCK.advance(40)
    check("close series: not while LOCKED (and no hold)", len(lst.wakes) <= 1,
          lst.wakes)


def t_hold_resync_coalesces():
    hass, ctl, wm, lst = make_real()
    ctl._state = ctl._logged_state = "locked"
    t0 = CLOCK.now
    door(hass, ctl, "on")          # forced wake at 0, hold
    CLOCK.advance(4)
    door(hass, ctl, "off")         # close during the hold: series 14/24/34
    CLOCK.advance(controller_mod.DOOR_HOLD_S + 10)
    got = [round(w - t0, 1) for w in lst.wakes]
    check("hold: +15 re-sync merges into the +14 close wake (coalescing)",
          got == [0.0, 14.0, 24.0, 34.0] and wm.stats["merged"] >= 1, got)


def t_hold_dp11_only_flush():
    """A flush with ONLY a bare dp11:false (no method DP): no state row before
    the hold resolves; then thumbturn line, then the row."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "on")
    CLOCK.advance(3)
    raw(ctl, type="state_report", dt=CLOCK.now - 4, locked=False)
    CLOCK.advance(5)
    check("hold: bare dp11:false during a hold writes no state row",
          not state_changes(hass) and ctl._state == "locked", state_changes(hass))
    CLOCK.advance(controller_mod.DOOR_HOLD_S)
    check("hold: ...the hold still resolves line-then-row",
          timeline(hass) == [("line", "Unlocked (thumbturn)"), ("state", "unlocked")],
          timeline(hass))


def t_hold_reopen_and_mark_locked():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "on")
    hold = ctl._door_hold
    CLOCK.advance(3)
    door(hass, ctl, "off")
    CLOCK.advance(2)
    door(hass, ctl, "on")
    check("hold: a reopen during the hold keeps the SAME hold (no second one, "
          "no state row)", ctl._door_hold is hold and hold.closed_t is None
          and not state_changes(hass))
    CLOCK.advance(controller_mod.DOOR_HOLD_S)
    check("hold: reopened hold still resolves once (line then row)",
          timeline(hass) == [("line", "Unlocked (thumbturn)"), ("state", "unlocked")],
          timeline(hass))
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "on")
    CLOCK.advance(5)
    ctl.mark_locked("uid-julie")
    CLOCK.advance(controller_mod.DOOR_HOLD_S)
    check("hold: 'Mark as locked' during a hold (newer assertion) wins: the hold "
          "logs its line but doesn't unlock",
          ctl._state == "locked"
          and [r[0] for r in activity_rows(hass)]
          == ["Marked as locked in Home Assistant", "Unlocked (thumbturn)"],
          (ctl._state, activity_rows(hass)))


def t_lock_entity_snapshot():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    ctl._changed_by = "Device locked"
    ctl._inferred = False
    coord = ctl.coordinator
    coord.controller = ctl
    ent = H.lock_mod.EraLockEntity(coord, types.SimpleNamespace(
        entry_id="E1", unique_id="HUB"))
    ent.hass = hass
    ent.rows = []
    ent.async_on_remove = lambda f: None
    asyncio.run(ent.async_added_to_hass())
    check("lock entity: starts from the controller's current state (reported "
          "before the platform was set up)",
          ent.is_locked is True and ent.changed_by == "Device locked"
          and ent.available)
    ctl.set_auth_failed("expired")
    check("lock entity: auth failure -> unavailable",
          ent.available is False and ctl.snapshot()["available"] is False)


# ------------------------------------------------------------- reauth flow --
def _load_config_flow():
    req = H.mod("requests", RequestException=type("RequestException", (Exception,), {}))

    class _Schema:
        def __init__(self, *a, **k):
            pass
    marker = lambda *a, **k: a[0] if a else None  # noqa: E731
    H.mod("voluptuous", Schema=_Schema, Required=marker, Optional=marker,
          In=lambda x: x)

    class ConfigFlow:
        def __init_subclass__(cls, domain=None, **kw):
            super().__init_subclass__(**kw)

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_update_and_abort(self, entry, *, data):
            entry.data = data
            self.calls.append("update_and_abort")
            for f in list(entry.update_listeners):
                self.hass.pending.append(f(self.hass, entry))
            return {"type": "abort", "reason": "reauth_successful"}

        def async_update_reload_and_abort(self, entry, *, data):
            entry.data = data
            self.calls.append("update_reload_and_abort")
            return {"type": "abort", "reason": "reauth_successful"}

    ce = sys.modules["homeassistant.config_entries"]
    ce.ConfigFlow, ce.OptionsFlow, ce.ConfigFlowResult = ConfigFlow, object, dict
    hconst.CONF_PASSWORD, hconst.CONF_USERNAME = "password", "username"
    sel = H.mod("homeassistant.helpers.selector", **{n: (lambda *a, **k: None) for n in (
        "EntitySelector", "EntitySelectorConfig", "NumberSelector",
        "NumberSelectorConfig", "TextSelector", "TextSelectorConfig")},
        NumberSelectorMode=types.SimpleNamespace(BOX="box"),
        TextSelectorType=types.SimpleNamespace(PASSWORD="password", TEXT="text"))
    del req, sel
    return importlib.import_module("era_lock.config_flow")


def t_reauth_flow():
    cf = _load_config_flow()
    new_session = esession.Session(uid="U", partner="P", ch_key="CH", token="tokR",
                                   ecode="ecR")
    cf._validate_sync = lambda email, pw, combined: {
        "refresh_token": "RT-NEW", "session": new_session}
    # (a) entry LOADED, the session died at runtime -> the update listener
    # (present) reloads: data-only update, no HA double reload.
    hass, entry, out = setup()
    flow = cf.EraLockConfigFlow()
    flow.hass, flow.calls = hass, []
    flow._get_reauth_entry = lambda: entry

    async def go():
        res = await flow.async_step_reauth_confirm({"password": "pw"})
        await drain(hass)
        return res
    res = asyncio.run(go())
    check("reauth (loaded entry): new credentials stored, update listener reloads once",
          res["reason"] == "reauth_successful" and flow.calls == ["update_and_abort"]
          and entry.data["era"]["refresh_token"] == "RT-NEW"
          and entry.data["session"]["token"] == "tokR"
          and hass.config_entries.reloads == ["E1"],
          (flow.calls, hass.config_entries.reloads))
    # (b) setup failed with ConfigEntryAuthFailed (no update listener) -> the
    # flow reloads itself.
    hass, entry, out = setup(codes=(5,), auth={"refresh": era_stub.EraAuthError("x")})
    AUTH["refresh"] = None
    flow = cf.EraLockConfigFlow()
    flow.hass, flow.calls = hass, []
    flow._get_reauth_entry = lambda: entry
    res = asyncio.run(flow.async_step_reauth_confirm({"password": "pw"}))
    check("reauth (setup failed): no update listener -> update + reload + abort",
          isinstance(out.get("err"), H.ConfigEntryAuthFailed)
          and not entry.update_listeners
          and flow.calls == ["update_reload_and_abort"]
          and entry.data["session"]["token"] == "tokR", (out, flow.calls))


def t_setup_restores_state():
    """v0.1.4: the stored state is seeded BEFORE the listener starts and the
    startup sync is sent, and written back on HA stop / unload."""
    seen = {}
    ctl_cls = init.EraController
    real_ctl_start = ctl_cls.start

    def spy_ctl_start(self):
        seen["state_at_controller_start"] = self._state
        return real_ctl_start(self)
    ctl_cls.start = spy_ctl_start
    try:
        hass, entry, out = setup(stored={"state": "locked", "last_method": "card",
                                         "last_user": "Spare card",
                                         "last_device_dt": CLOCK.now - 100,
                                         "infer_t": 0.0})
    finally:
        ctl_cls.start = real_ctl_start
    c = entry.runtime_data
    ctl = c.controller
    client = FAKE_CLIENTS[-1]
    # (async_setup_entry calls controller.start() before listener.start)
    check("restore setup: seeded before the controller subscribes / listener starts",
          seen.get("state_at_controller_start") == "locked", seen)
    check("restore setup: LOCKED, inferred, 'Restored after restart'; sync still sent",
          out.get("ok") is True and ctl._state == "locked" and ctl._inferred
          and ctl._changed_by == "Restored after restart"
          and dps_sent(client) == [{"54": "Aw=="}], (out, ctl.diagnostics()))
    # The startup sync's replay lands on top of it through the normal path.
    raw(ctl, type="unlock", dt=CLOCK.now - 50, fresh=False, method="card", cred=51)
    raw(ctl, type="state_report", dt=CLOCK.now - 50, fresh=False, locked=False)
    CLOCK.advance(3)
    check("restore setup: buffered replay overrides the restored state",
          ctl._state == "unlocked" and not ctl._inferred, ctl.diagnostics())
    et, cb = hass.stop_listeners[0]
    asyncio.run(cb(None))
    check("restore setup: HA stop writes the current state for the next start",
          H.STORE_DISK["era_lock.E1"]["state"] == "unlocked"
          and H.STORE_DISK["era_lock.E1"]["last_device_dt"] == CLOCK.now - 3 - 50,
          H.STORE_DISK.get("era_lock.E1"))
    asyncio.run(init.async_remove_entry(hass, entry))
    check("restore setup: removing the entry drops its stored state",
          "era_lock.E1" not in H.STORE_DISK)
    hass, entry, out = setup()
    check("restore setup: nothing stored -> unknown, as before",
          entry.runtime_data.controller._state is None)


ALL_TESTS = [t_setup_connected, t_setup_stale_session_persisted, t_setup_auth_failed,
             t_setup_transport_not_ready, t_runtime_auth_failure,
             t_runtime_transport_failure, t_update_listener_rules, t_shutdown,
             t_wake_hygiene, t_heartbeat, t_battery_sensor, t_diagnostics_redaction,
             t_close_series, t_hold_resync_coalesces, t_hold_dp11_only_flush,
             t_hold_reopen_and_mark_locked, t_lock_entity_snapshot, t_reauth_flow,
             t_setup_restores_state]

if __name__ == "__main__":
    sys.exit(H.run(ALL_TESTS))
