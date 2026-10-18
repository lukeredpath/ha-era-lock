"""Offline, HA-free tests for the ERA lock state engine + activity attribution.

Stubs the handful of homeassistant APIs the integration touches, loads the REAL
controller / activity / event-entity / logbook / listener modules from the
package (without running its __init__), and drives them with a fake clock +
scheduler and a fake paho client/broker. Stdlib only.

Also the shared harness for the other tests in this directory (they import it;
the suite below only runs when this file is executed directly).

Run (from the repo root):  python3 -I -B tests/offline/test_controller_harness.py
Optionally pass a package dir:  ... test_controller_harness.py path/to/era_lock
"""
from __future__ import annotations

import importlib
import os
import sys
import types
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG_DIR = os.path.abspath(
    (sys.argv[1] if __name__ == "__main__" and len(sys.argv) > 1 else None)
    or os.environ.get("ERA_LOCK_PKG")
    or os.path.join(_REPO, "custom_components", "era_lock"))

# ---------------------------------------------------------------- fake clock --
class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0
        self.timers = []  # [due, seq, cb, alive]
        self.seq = 0

    def time(self):
        return self.now

    def call_later(self, delay, cb):
        self.seq += 1
        t = [self.now + delay, self.seq, cb, True]
        self.timers.append(t)

        def cancel():
            t[3] = False
        return cancel

    def advance(self, secs):
        end = self.now + secs
        while True:
            due = sorted((t for t in self.timers if t[3] and t[0] <= end),
                         key=lambda t: (t[0], t[1]))
            if not due:
                break
            t = due[0]
            t[3] = False
            self.now = max(self.now, t[0])
            t[2](self.now)
        self.now = end


CLOCK = Clock()

# ----------------------------------------------------------------- HA stubs --
def mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class Context:
    def __init__(self, user_id=None, parent_id=None, id=None):  # noqa: A002
        self.id = id or uuid.uuid4().hex
        self.user_id = user_id
        self.parent_id = parent_id


class Event:
    def __init__(self, event_type, data, context=None):
        self.event_type, self.data, self.context = event_type, data, context


def callback(f):
    return f


class HomeAssistantError(Exception):
    """Mimics HA's: optional translation_domain/key (+ placeholders)."""

    def __init__(self, *args, translation_domain=None, translation_key=None,
                 translation_placeholders=None):
        if not args and translation_key and translation_domain:
            args = (translation_key,)
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


class ServiceValidationError(HomeAssistantError):
    """Mimics HA's: a refusal of the request (not logged as an error)."""


class ConfigEntryAuthFailed(HomeAssistantError):
    pass


class ConfigEntryNotReady(HomeAssistantError):
    pass


DISPATCH: dict[str, list] = {}


def async_dispatcher_connect(hass, signal, target):
    DISPATCH.setdefault(signal, []).append(target)
    return lambda: DISPATCH[signal].remove(target)


def async_dispatcher_send(hass, signal, *args):
    for t in list(DISPATCH.get(signal, [])):
        t(*args)


def async_call_later(hass, delay, cb):
    return CLOCK.call_later(delay, cb)


def async_track_state_change_event(hass, entity_ids, cb):
    hass._door_cb = cb
    return lambda: None


class Platform:
    LOCK = "lock"; SENSOR = "sensor"; BUTTON = "button"; EVENT = "event"
    BINARY_SENSOR = "binary_sensor"


class EventEntity:
    hass = None
    entity_id = "event.era_protect_lock_activity"
    registry_entry = types.SimpleNamespace(device_id="dev-1")

    def async_set_context(self, ctx):
        self._context = ctx

    def _trigger_event(self, etype, attrs):
        self.hass.log.append(("event_entity", etype, dict(attrs),
                              getattr(self, "_context", None)))

    def async_write_ha_state(self):
        pass

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, f):
        pass


class Registry:
    def async_get_entity_id(self, domain, platform, unique_id):
        return "lock.era_protect_lock" if domain == "lock" else None


mod("homeassistant")
mod("homeassistant.core", Context=Context, Event=Event, HomeAssistant=object,
    callback=callback)
mod("homeassistant.exceptions", HomeAssistantError=HomeAssistantError,
    ServiceValidationError=ServiceValidationError,
    ConfigEntryAuthFailed=ConfigEntryAuthFailed,
    ConfigEntryNotReady=ConfigEntryNotReady)
mod("homeassistant.const", Platform=Platform,
    EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic", CONFIG="config"))
mod("homeassistant.helpers")
mod("homeassistant.helpers.dispatcher",
    async_dispatcher_connect=async_dispatcher_connect,
    async_dispatcher_send=async_dispatcher_send)
mod("homeassistant.helpers.event", async_call_later=async_call_later,
    async_track_state_change_event=async_track_state_change_event,
    async_track_time_interval=lambda hass, cb, interval: (lambda: None))
er = mod("homeassistant.helpers.entity_registry", async_get=lambda hass: Registry())
sys.modules["homeassistant.helpers"].entity_registry = er
mod("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
mod("homeassistant.config_entries", ConfigEntry=object)
mod("homeassistant.components")
pn = mod("homeassistant.components.persistent_notification",
         async_create=lambda hass, msg, title=None, notification_id=None:
         hass.log.append(("notify", notification_id)))
sys.modules["homeassistant.components"].persistent_notification = pn
mod("homeassistant.components.event", EventEntity=EventEntity)

CONTEXT_RECENT_TIME_SECONDS = 5  # HA helpers/entity.py


class LockEntity:
    """Mimics the bits of HA's Entity/LockEntity that matter here: the state
    precedence (unlocking before locked/unlocked), entity service calls skipping
    unavailable entities, and the context expiry on write."""
    hass = None
    _context = None
    _context_set = None

    @property
    def state(self):
        if self.is_unlocking:
            return "unlocking"
        locked = self.is_locked
        if locked is None:
            return None
        return "locked" if locked else "unlocked"

    def async_set_context(self, ctx):
        self._context = ctx
        self._context_set = CLOCK.now

    def async_write_ha_state(self):
        if (self._context_set is not None
                and CLOCK.now - self._context_set > CONTEXT_RECENT_TIME_SECONDS):
            self._context = None
            self._context_set = None
        ctx = self._context or Context()  # HA mints a fresh one when none
        st = self.state if self.available else "unavailable"
        self.rows.append((st, ctx, CLOCK.now, dict(self.extra_state_attributes)))

    def async_on_remove(self, f):
        pass


class CoordinatorEntity:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator

    async def async_added_to_hass(self):
        pass


mod("homeassistant.components.lock", LockEntity=LockEntity)


class BinarySensorEntity:
    hass = None
    _attr_is_on = None
    _context = None

    @property
    def is_on(self):
        return self._attr_is_on

    def async_set_context(self, ctx):
        self._context = ctx

    def async_write_ha_state(self):
        st = ("on" if self.is_on else "off") if self.available else "unavailable"
        self.rows.append((st, self._context or Context()))
        self._context = None

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, f):
        pass


mod("homeassistant.components.binary_sensor", BinarySensorEntity=BinarySensorEntity,
    BinarySensorDeviceClass=types.SimpleNamespace(CONNECTIVITY="connectivity"))
mod("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity)


class ButtonEntity:
    hass = None
    _context = None

    def async_set_context(self, ctx):
        self._context = ctx


mod("homeassistant.components.button", ButtonEntity=ButtonEntity)
mod("homeassistant.components.logbook", LOGBOOK_ENTRY_ENTITY_ID="entity_id",
    LOGBOOK_ENTRY_ICON="icon", LOGBOOK_ENTRY_MESSAGE="message",
    LOGBOOK_ENTRY_NAME="name")
mod("homeassistant.util")
mod("homeassistant.util.dt", utc_from_timestamp=lambda t: types.SimpleNamespace(
    isoformat=lambda: f"T{t}"))
sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]

# Fake package (so relative imports work without executing __init__.py).
pkg = types.ModuleType("era_lock")
pkg.__path__ = [PKG_DIR]
pkg.EraConfigEntry = object
sys.modules["era_lock"] = pkg
mod("era_lock.eralock")
sys.modules["era_lock.eralock"].__path__ = [PKG_DIR + "/eralock"]  # real dpmap


# eralock/era.py needs `requests`; the error taxonomy is all the component
# imports from it (test_cognito.py tests the real module).
class EraError(RuntimeError):
    pass


class EraAuthError(EraError):
    pass


class EraConnectionError(EraError):
    pass


mod("era_lock.eralock.era", EraError=EraError, EraAuthError=EraAuthError,
    EraConnectionError=EraConnectionError)
mod("era_lock.coordinator", EraLockCoordinator=object)
mod("era_lock.session", EraSessionManager=object)

# --- fake paho + broker: records every client created and every publish; the
# "broker" answers command frames on a separate thread (like paho's network
# thread) by calling the owning client's on_message.
import threading as _threading
import time as _rtime

FAKE_CLIENTS: list = []


class FakeInfo:
    def __init__(self, mid, rc=0, published=True):
        self.mid, self.rc, self._pub = mid, rc, published

    def is_published(self):
        if self.rc:
            raise RuntimeError("Message publish failed: not connected")
        return self._pub


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


class FakeClient:
    def __init__(self, api=None, client_id=None, clean_session=True, protocol=None):
        self.client_id = client_id
        self.published = []           # (topic, decoded json dict)
        self.creds = None
        self.creds_history = []
        self._out_message_mutex = _threading.RLock()
        self._out_messages = {}
        self._inflight_messages = 0
        self._mid = 0
        self.responder = None         # fn(client, dps) -> list[(delay, frame dict)]
        self.publish_rc = 0           # force e.g. NO_CONN (4)
        self.publish_unacked = False  # leave frames un-PUBACKed
        self.on_creds = None          # fn(client) when username_pw_set is called
        self.on_connect = self.on_message = self.on_disconnect = None
        self.reconnects = 0
        FAKE_CLIENTS.append(self)

    def username_pw_set(self, u, p):
        self.creds = (u, p)
        self.creds_history.append((u, p))
        if self.on_creds:
            self.on_creds(self)

    def tls_set(self): pass
    def reconnect_delay_set(self, **kw): pass
    def connect_async(self, *a, **kw): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, topic, qos=0): pass

    def reconnect(self):
        self.reconnects += 1

    def connect(self, *a, **kw):
        # standalone CloudMqttLock: connect then CONNACK on "network thread"
        _threading.Timer(0.02, lambda: self.on_connect(self, None, {}, 0, None)).start()

    def publish(self, topic, payload, qos=0):
        import json as _j
        self._mid += 1
        body = payload[15:]
        frame = _j.loads(body[:-body[-1]].decode())  # fake AES = identity
        self.published.append((topic, frame))
        info = FakeInfo(self._mid, rc=self.publish_rc,
                        published=not (self.publish_unacked or self.publish_rc))
        if self.publish_rc or self.publish_unacked:
            with self._out_message_mutex:
                self._out_messages[self._mid] = types.SimpleNamespace(
                    state=1 if self.publish_rc else 2)
                if not self.publish_rc:
                    self._inflight_messages += 1
        if self.responder and not self.publish_rc:
            for delay, reply in self.responder(self, frame["data"]["dps"]):
                _threading.Timer(delay, self.deliver, (reply,)).start()
        return info

    def deliver(self, frame, topic="smart/mb/in/CID1"):
        import json as _j
        pt = _j.dumps(frame).encode()
        pad = 16 - len(pt) % 16
        self.on_message(self, None, FakeMsg(topic, b"2.2" + b"\0" * 12 + pt + bytes([pad]) * pad))


class FakeAES:
    MODE_ECB = 1

    @staticmethod
    def new(key, mode):
        return types.SimpleNamespace(encrypt=lambda b: b, decrypt=lambda b: b)


mod("paho"); mod("paho.mqtt")
mod("paho.mqtt.client", Client=FakeClient, MQTTv311=4, mqtt_ms_wait_for_puback=2,
    CallbackAPIVersion=types.SimpleNamespace(VERSION2=2))
sys.modules["paho"].mqtt = sys.modules["paho.mqtt"]
sys.modules["paho.mqtt"].client = sys.modules["paho.mqtt.client"]
mod("Crypto"); mod("Crypto.Cipher", AES=FakeAES)
sys.modules["Crypto"].Cipher = sys.modules["Crypto.Cipher"]

mqtt_mod = importlib.import_module("era_lock.eralock.mqtt")  # the REAL module
MqttUnlockError = mqtt_mod.MqttUnlockError

activity = importlib.import_module("era_lock.activity")
events = importlib.import_module("era_lock.events")
controller_mod = importlib.import_module("era_lock.controller")
event_mod = importlib.import_module("era_lock.event")
logbook_mod = importlib.import_module("era_lock.logbook")
const = importlib.import_module("era_lock.const")
wake_mod = importlib.import_module("era_lock.wake")
lock_mod = importlib.import_module("era_lock.lock")
listener_mod = importlib.import_module("era_lock.mqtt_listener")
bs_mod = importlib.import_module("era_lock.binary_sensor")
button_mod = importlib.import_module("era_lock.button")
controller_mod.time = CLOCK  # controller only uses time.time()
wake_mod.time = types.SimpleNamespace(monotonic=CLOCK.time, time=CLOCK.time)
LockEvent = events.LockEvent


# ------------------------------------------------------------------ harness --
class Bus:
    def __init__(self, hass):
        self.hass = hass

    def async_fire(self, etype, data, context=None):
        self.hass.log.append(("bus", etype, dict(data), context))


class Wake:
    def __init__(self, hass):
        self.hass = hass

    def on_unlock(self):
        self.hass.log.append(("wake", "on_unlock"))

    def cancel_polls(self):
        self.hass.log.append(("wake", "cancel_polls"))

    def on_door_change(self, force=False):
        self.hass.log.append(("wake", "door", force))

    def schedule_close_wakes(self, elapsed=0.0):
        self.hass.log.append(("wake", "close_series", CLOCK.now))

    def cancel_close_wakes(self):
        self.hass.log.append(("wake", "cancel_close_wakes"))

    def hold_resync(self):
        self.hass.log.append(("wake", "hold_resync", CLOCK.now))

    def on_reconnect(self):
        self.hass.log.append(("wake", "on_reconnect"))


CREDS = [
    {"method": "fingerprint", "id": 1, "label": "Luke's thumb",
     "user_id": "uid-luke", "user_name": "Luke"},
    {"method": "card", "id": 51, "label": "Spare card"},
    {"method": "fingerprint", "id": 2, "label": ""},
]


def make():
    global DISPATCH
    DISPATCH.clear()
    CLOCK.timers.clear()
    hass = types.SimpleNamespace(log=[], states=types.SimpleNamespace(
        get=lambda eid: hass._door_state))
    hass._door_state = None
    hass.bus = Bus(hass)
    hass.config_entries = types.SimpleNamespace(
        async_update_entry=lambda entry, options: setattr(entry, "options", options))
    entry = types.SimpleNamespace(entry_id="E1", unique_id="HUB",
                                  options={"credentials": [dict(c) for c in CREDS],
                                           "door_contact_entity_id": "binary_sensor.door"})
    coord = types.SimpleNamespace(wake=Wake(hass))
    ctl = controller_mod.EraController(hass, entry, coord)
    ctl.start()
    ent = event_mod.EraLockEventEntity(entry)
    ent.hass = hass
    async_dispatcher_connect(hass, events.signal_event("E1"), ent._on_event)
    states = []

    def _state(p):
        states.append(p)
        hass.log.append(("state", p, CLOCK.now))
    async_dispatcher_connect(hass, events.signal_state("E1"), _state)
    hass.states_log = states
    hass.async_add_executor_job = lambda f, *a: f(*a)
    # describer
    described = {}
    logbook_mod.async_describe_events(
        hass, lambda d, et, fn: described.__setitem__(et, fn))
    hass.describe = lambda e: described[e.event_type](e)
    return hass, ctl


def activity_rows(hass):
    """The rich logbook lines: (message, user_id, ctx_id, entity_id)."""
    out = []
    for rec in hass.log:
        if rec[0] == "bus" and rec[1] == const.EVENT_ACTIVITY:
            d = hass.describe(Event(rec[1], rec[2], rec[3]))
            out.append((d["message"], rec[3].user_id, rec[3].id, d.get("entity_id")))
    return out


def state_changes(hass):
    """Lock state publishes that start a new row (cause ctx set)."""
    return [s for s in hass.states_log if s["cause_ctx_id"]]


def timeline(hass):
    """Activity lines and NEW state rows, in emission order:
    ("line", message) / ("state", state)."""
    out = []
    for rec in hass.log:
        if rec[0] == "bus" and rec[1] == const.EVENT_ACTIVITY:
            out.append(("line", hass.describe(Event(rec[1], rec[2], rec[3]))["message"]))
        elif rec[0] == "state" and rec[1]["cause_ctx_id"]:
            out.append(("state", rec[1]["state"]))
    return out


def state_row_times(hass):
    return [rec[2] for rec in hass.log if rec[0] == "state" and rec[1]["cause_ctx_id"]]


def followups(hass):
    """Post-close wake series started: ("wake", "close_series", at)."""
    return [r for r in hass.log if r[:2] == ("wake", "close_series")]


def door(hass, ctl, state, old=None):
    # A real door transition carries the previous state; default to the
    # opposite of the new one (startup-shaped events with no old_state are
    # deliberately ignored by the controller — see t_door_startup_ignored).
    prev = getattr(hass, "_door_state", None)
    if old is None:
        old = prev.state if prev is not None else ("off" if state == "on" else "on")
    old_state = types.SimpleNamespace(state=old) if old != "none" else None
    hass._door_state = types.SimpleNamespace(state=state)
    hass._door_cb(Event("state_changed",
                        {"new_state": hass._door_state, "old_state": old_state}))


def raw(ctl, **kw):
    ctl._on_raw(LockEvent(**kw))


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{detail}]" if detail and not cond else ""))


# ------------------------------------------------------------------- tests --
def t_messages():
    d = activity.describe
    cases = [
        (d("unlock", method="fingerprint", cred=1, label="Luke's thumb",
           user_name="Luke", user_mapped=True), "Unlocked by Luke (fingerprint)"),
        (d("unlock", method="card", cred=51, label="Spare card"),
         "Unlocked by Spare card (card)"),
        (d("unlock", method="fingerprint", cred=2), "Unlocked (fingerprint #2)"),
        (d("unlock", method="remote"), "Unlocked (remote)"),
        (d("unlock", method="phone_remote", cred=0), "Unlocked (phone remote)"),
        (d("unlock", method="remote", source="command"), "Unlocked in Home Assistant"),
        (d("unlock", method="thumbturn", source="door"), "Unlocked (thumbturn)"),
        (d("unlock", method="fingerprint", cred=3, who="Julie"),
         "Unlocked by Julie (fingerprint)"),
        (d("lock", source="device"), "Device locked"),
        # v0.1.2: the failsafe only ASSUMES a lock (door stayed shut), and the
        # manual assertion is the "Mark as locked" button (no lock command).
        (d("lock", source="timer"), "Auto-locked (assumed)"),
        (d("lock", source="manual"), "Marked as locked in Home Assistant"),
        (d("state_unconfirmed", source="timer"), "Lock state not confirmed"),
        (d("failed_unlock", method="fingerprint"), "Failed fingerprint attempt"),
        (d("tamper", subtype="hijack"), "Tamper: hijack"),
        (d("tamper", subtype="pry_alarm"), "Tamper: pry alarm"),
        (d("door", subtype="open"), "Door opened"),
        (d("door", subtype="close"), "Door closed"),
    ]
    for got, want in cases:
        check(f"message: {want!r}", got == want, got)


def t_fingerprint_state_change():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t = CLOCK.now
    raw(ctl, type="unlock", dt=t, method="fingerprint", cred=1)
    raw(ctl, type="state_report", dt=t, locked=False)
    CLOCK.advance(3)
    rows = activity_rows(hass)
    sc = state_changes(hass)
    check("fp unlock: one rich row 'Unlocked by Luke (fingerprint)' By Luke on the lock",
          [(r[0], r[1], r[3]) for r in rows]
          == [("Unlocked by Luke (fingerprint)", "uid-luke", "lock.era_protect_lock")], rows)
    check("fp unlock: state -> unlocked links to the emitted row's ctx, By Luke",
          len(sc) == 1 and sc[0]["state"] == "unlocked"
          and sc[0]["cause_ctx_id"] == rows[0][2] and sc[0]["user_id"] == "uid-luke"
          and sc[0]["changed_by"] == "Unlocked by Luke (fingerprint)", sc)
    ee = [r for r in hass.log if r[0] == "event_entity"]
    check("fp unlock: event entity fired 'unlock' with description + same ctx",
          len(ee) == 1 and ee[0][1] == "unlock"
          and ee[0][2]["description"] == "Unlocked by Luke (fingerprint)"
          and ee[0][3].id == rows[0][2], ee)
    check("no legacy era_lock_cause events fired",
          not any(r[0] == "bus" and r[1] == const.EVENT_CAUSE for r in hass.log))
    # bus row fired before the event entity's state write (logbook root row)
    idx_bus = next(i for i, r in enumerate(hass.log) if r[0] == "bus")
    idx_ee = next(i for i, r in enumerate(hass.log) if r[0] == "event_entity")
    check("activity bus row precedes event entity write", idx_bus < idx_ee)


def t_netted_fingerprint():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    t = CLOCK.now
    raw(ctl, type="unlock", dt=t, method="fingerprint", cred=2)
    CLOCK.advance(3)
    rows = activity_rows(hass)
    check("netted fp (already unlocked): still one rich row 'Unlocked (fingerprint #2)'",
          [r[0] for r in rows] == ["Unlocked (fingerprint #2)"], rows)
    check("netted fp: no lock state row", not state_changes(hass), state_changes(hass))


def t_buffered_flush_nets_to_locked():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t = CLOCK.now
    # buffered (stale) card unlock then device relock, replayed on wake
    raw(ctl, type="unlock", dt=t - 100, fresh=False, method="card", cred=51)
    raw(ctl, type="state_report", dt=t - 100, fresh=False, locked=False)
    raw(ctl, type="state_report", dt=t - 90, fresh=False, locked=True)
    CLOCK.advance(3)
    rows = [r[0] for r in activity_rows(hass)]
    check("buffered unlock->relock: rows 'Unlocked by Spare card (card)', 'Device locked'",
          rows == ["Unlocked by Spare card (card)", "Device locked"], rows)
    check("buffered unlock->relock: no state flicker (stays locked, no row)",
          not state_changes(hass) and ctl._state == "locked", state_changes(hass))
    check("buffered: no live side effects (no polls)",
          ("wake", "on_unlock") not in hass.log)


def t_remote_dp61_dp62():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t = CLOCK.now
    raw(ctl, type="unlock", dt=t, method="remote")            # dp61 echo first
    CLOCK.advance(1)
    raw(ctl, type="unlock", dt=t, method="phone_remote", cred=0)  # dp62, same dt
    CLOCK.advance(5)
    rows = activity_rows(hass)
    sc = state_changes(hass)
    check("remote dp61+dp62: exactly one rich row 'Unlocked (phone remote)'",
          [r[0] for r in rows] == ["Unlocked (phone remote)"], rows)
    check("remote dp61+dp62: state links to the EMITTED (phone_remote) row's ctx",
          len(sc) == 1 and sc[0]["cause_ctx_id"] == rows[0][2], (sc, rows))
    check("remote dp61+dp62: reason/last_method use phone_remote",
          sc and sc[-1]["changed_by"] == "Unlocked (phone remote)"
          and sc[-1]["last_method"] == "phone_remote", sc)


def t_remote_dp62_after_apply():
    """dp62 arrives after the 2s debounce already applied the echo's state."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t = CLOCK.now
    raw(ctl, type="unlock", dt=t, method="remote")
    CLOCK.advance(1.9)  # echo hold (2s) still open
    # debounce fires at +2.0 before the dp62 below? push dp62 at +1.95
    CLOCK.advance(0.05)
    raw(ctl, type="unlock", dt=t + 0.5, method="phone_remote", cred=0)
    CLOCK.advance(5)
    rows = activity_rows(hass)
    sc = state_changes(hass)
    check("dp62 late (dt differs): one row, state links to it",
          [r[0] for r in rows] == ["Unlocked (phone remote)"]
          and len(sc) == 1 and sc[0]["cause_ctx_id"] == rows[0][2], (rows, sc))


def t_lone_dp61():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="remote")
    CLOCK.advance(5)
    rows = activity_rows(hass)
    sc = state_changes(hass)
    check("lone dp61: emits 'Unlocked (remote)' and state links to it",
          [r[0] for r in rows] == ["Unlocked (remote)"] and len(sc) == 1
          and sc[0]["cause_ctx_id"] == rows[0][2], (rows, sc))


def t_bare_dp11_wins_ordering():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t = CLOCK.now
    raw(ctl, type="state_report", dt=t, locked=False)       # same frame, first
    raw(ctl, type="unlock", dt=t, method="fingerprint", cred=1)
    CLOCK.advance(3)
    rows = activity_rows(hass)
    sc = state_changes(hass)
    check("dp11:false before dp12: state still links to the fingerprint row, By Luke",
          len(sc) == 1 and sc[0]["cause_ctx_id"] == rows[0][2]
          and sc[0]["user_id"] == "uid-luke"
          and sc[0]["changed_by"] == "Unlocked by Luke (fingerprint)", (rows, sc))



def t_door_hold_fingerprint():
    """v0.1.3 (goal 8): opening from LOCKED writes NOTHING until the flush says
    how; a fingerprint flushed 2s after the open resolves the hold at once
    (DOOR_SETTLE_S later): its line, THEN the unlocked row parented to it."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t_fp = CLOCK.now
    CLOCK.advance(4)
    t_open = CLOCK.now
    door(hass, ctl, "on")  # opened while locked; fp still buffered
    check("door hold: no state row at the open (state stays locked), forced wake",
          not state_changes(hass) and ctl._state == "locked"
          and ("wake", "door", True) in hass.log, (hass.states_log, hass.log))
    check("door hold: no activity row yet", not activity_rows(hass))
    CLOCK.advance(2)  # flush lands 2s after the open
    raw(ctl, type="state_report", dt=t_fp, locked=False)   # dp11 first in the frame
    raw(ctl, type="unlock", dt=t_fp, method="fingerprint", cred=1)
    CLOCK.advance(controller_mod.DOOR_SETTLE_S / 2)
    check("door hold + fp: nothing written inside the flush settle",
          not activity_rows(hass) and not state_changes(hass),
          (activity_rows(hass), state_changes(hass)))
    CLOCK.advance(controller_mod.DOOR_SETTLE_S)
    rows = activity_rows(hass)
    sc = state_changes(hass)
    check("door hold + buffered fp: ONE unlock row 'Unlocked by Luke (fingerprint)', no thumbturn",
          [r[0] for r in rows] == ["Unlocked by Luke (fingerprint)"], rows)
    check("door hold + fp: line FIRST, then ONE unlocked row parented to it, By Luke",
          timeline(hass) == [("line", "Unlocked by Luke (fingerprint)"),
                             ("state", "unlocked")]
          and sc[0]["cause_ctx_id"] == rows[0][2] and sc[0]["user_id"] == "uid-luke",
          (timeline(hass), sc))
    check("door hold + fp: resolved ~2s after the open (not at DOOR_HOLD_S)",
          state_row_times(hass)[0] - t_open
          <= 2 + controller_mod.DOOR_SETTLE_S + 0.01, state_row_times(hass))
    last = hass.states_log[-1]
    check("door hold + fp: reason, last_method fingerprint, by Luke, device-reported",
          last["changed_by"] == "Unlocked by Luke (fingerprint)"
          and last["last_method"] == "fingerprint" and last["last_user"] == "Luke"
          and last["inferred"] is False and ctl._state == "unlocked", last)
    CLOCK.advance(controller_mod.DOOR_HOLD_RESYNC_S)
    check("door hold + fp: resolved, so no mid-hold re-sync",
          not any(r[:2] == ("wake", "hold_resync") for r in hass.log))
    door(hass, ctl, "off")
    check("v0.1.3: a credential ENTRY's close also starts the post-close wakes",
          len(followups(hass)) == 1, followups(hass))
    CLOCK.advance(120)
    check("door hold + fp: still exactly one unlock row / state row afterwards",
          [r[0] for r in activity_rows(hass)] == ["Unlocked by Luke (fingerprint)"]
          and len(state_changes(hass)) == 1,
          (activity_rows(hass), state_changes(hass)))



def t_door_hold_thumbturn():
    """Nothing flushed: one extra sync mid-hold, then at DOOR_HOLD_S the
    thumbturn line and THEN the unlocked row parented to it."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t_open = CLOCK.now
    door(hass, ctl, "on")
    CLOCK.advance(controller_mod.DOOR_HOLD_RESYNC_S + 0.1)
    rs = [r for r in hass.log if r[:2] == ("wake", "hold_resync")]
    check("door hold (nothing flushed): one forced re-sync at DOOR_HOLD_RESYNC_S",
          len(rs) == 1 and abs(rs[0][2] - t_open - controller_mod.DOOR_HOLD_RESYNC_S)
          < 0.01, rs)
    CLOCK.advance(controller_mod.DOOR_HOLD_S - controller_mod.DOOR_HOLD_RESYNC_S - 0.6)
    check("door hold (nothing flushed): no row, no state change before the window expires",
          not activity_rows(hass) and not state_changes(hass)
          and ctl._state == "locked")
    CLOCK.advance(1)
    rows = activity_rows(hass)
    sc = state_changes(hass)
    check("door hold (nothing flushed): 'Unlocked (thumbturn)' after the window",
          [r[0] for r in rows] == ["Unlocked (thumbturn)"], rows)
    check("door hold thumbturn: line then unlocked row (parented), inferred, last_method thumbturn",
          timeline(hass) == [("line", "Unlocked (thumbturn)"), ("state", "unlocked")]
          and sc[0]["cause_ctx_id"] == rows[0][2] and sc[0]["inferred"] is True
          and hass.states_log[-1]["last_method"] == "thumbturn"
          and hass.states_log[-1]["changed_by"] == "Unlocked (thumbturn)",
          (timeline(hass), hass.states_log[-1]))
    check("door hold thumbturn: at DOOR_HOLD_S after the open",
          abs(state_row_times(hass)[0] - t_open - controller_mod.DOOR_HOLD_S) < 0.01,
          state_row_times(hass))


def t_door_hold_intervening_lock():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t0 = CLOCK.now
    CLOCK.advance(50)
    door(hass, ctl, "on")
    CLOCK.advance(3)
    # old fp at t0, then device relock at t0+10 -> fp can't explain the open
    raw(ctl, type="unlock", dt=t0, fresh=False, method="fingerprint", cred=2)
    raw(ctl, type="state_report", dt=t0 + 10, fresh=False, locked=True)
    CLOCK.advance(controller_mod.DOOR_HOLD_S)
    rows = [r[0] for r in activity_rows(hass)]
    check("door hold + fp disqualified by intervening lock: fp, lock, then thumbturn (in order)",
          rows == ["Unlocked (fingerprint #2)", "Device locked",
                   "Unlocked (thumbturn)"], rows)


def t_ha_commands():
    hass, ctl = make()
    import asyncio

    async def run():
        hass.async_add_executor_job = lambda f: asyncio.sleep(0)
        ctl.coordinator.async_request_refresh = lambda: asyncio.sleep(0)
        ctl._state = ctl._logged_state = "locked"
        await ctl.async_unlock("uid-julie", "Julie")
        raw(ctl, type="unlock", dt=CLOCK.now + 0.5, method="remote")  # its echo
        CLOCK.advance(5)
        ctl.mark_locked("uid-julie")  # v0.1.2: was async_lock
        await ctl.async_unlock("uid-julie", "Julie")
        CLOCK.advance(1)
        await ctl.async_unlock("uid-julie", "Julie")  # while already unlocked
    asyncio.run(run())
    rows = activity_rows(hass)
    check("HA commands: rows by the calling user, echo coalesced, repeat unlock kept",
          [(r[0], r[1]) for r in rows] == [
              ("Unlocked in Home Assistant", "uid-julie"),
              ("Marked as locked in Home Assistant", "uid-julie"),
              ("Unlocked in Home Assistant", "uid-julie"),
              ("Unlocked in Home Assistant", "uid-julie")], rows)


def t_timer_and_misc():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="card", cred=51)
    CLOCK.advance(3)
    hass._door_state = types.SimpleNamespace(state="off")
    CLOCK.advance(70)  # failsafe relock
    raw(ctl, type="failed_unlock", dt=CLOCK.now, method="fingerprint")
    raw(ctl, type="tamper", dt=CLOCK.now, subtype="hijack")
    rows = [r[0] for r in activity_rows(hass)]
    # v0.1.2: door sensor reporting shut throughout -> the failsafe ASSUMES the
    # auto-lock (was "Auto-locked (timer)").
    check("card unlock -> assumed auto-lock -> failed fp -> tamper rows",
          rows == ["Unlocked by Spare card (card)", "Auto-locked (assumed)",
                   "Failed fingerprint attempt", "Tamper: hijack"], rows)
    check("fresh unlock armed polls", ("wake", "on_unlock") in hass.log)


def t_dedupe():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    t = CLOCK.now
    raw(ctl, type="unlock", dt=t, method="fingerprint", cred=2)
    raw(ctl, type="unlock", dt=t, method="fingerprint", cred=2)  # re-flush
    CLOCK.advance(3)
    check("re-flushed identical unlock: one row (event entity de-dupe)",
          len(activity_rows(hass)) == 1, activity_rows(hass))



def t_door_startup_ignored():
    hass, ctl = make()
    door(hass, ctl, "off", old="none")  # sensor's first state write at startup
    check("door: startup write (no old_state) ignored - stays unknown",
          ctl._state is None and not state_changes(hass),
          (ctl._state, hass.states_log))


def t_door_close_never_unlocks_locked():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "off", old="on")
    check("door: close while LOCKED keeps locked (no 'Door closed' unlock)",
          ctl._state == "locked" and not state_changes(hass),
          (ctl._state, hass.states_log))


# ============================================================ fix 1: hold vs lock

def t_hold_then_live_lock_thumbturn():
    """Live: door opened while locked, closed at +6, handle lifted -> dp11:true
    live at +9 (inside the hold). Unlock line + unlocked row first, then the
    lock line + locked row."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "on")
    CLOCK.advance(6)
    door(hass, ctl, "off")
    CLOCK.advance(3)
    raw(ctl, type="state_report", dt=CLOCK.now, locked=True)  # live lock-up
    CLOCK.advance(controller_mod.DOOR_HOLD_S + 5)
    rows = activity_rows(hass)
    check("fix1: hold + live lock in window -> 'Unlocked (thumbturn)' THEN 'Device locked'",
          [r[0] for r in rows] == ["Unlocked (thumbturn)", "Device locked"], rows)
    check("fix1: no second unlock line when the hold timer would have expired",
          sum(1 for r in rows if r[0].startswith("Unlocked")) == 1, rows)
    sc = state_changes(hass)
    check("goal8: unlock line, unlocked row, lock line, locked row — in that order",
          timeline(hass) == [("line", "Unlocked (thumbturn)"), ("state", "unlocked"),
                             ("line", "Device locked"), ("state", "locked")]
          and sc[0]["cause_ctx_id"] == rows[0][2]
          and sc[1]["cause_ctx_id"] == rows[1][2], (timeline(hass), sc, rows))
    check("v0.1.3: the close (hold pending) started the post-close wakes; the "
          "settled lock cancelled the rest",
          len(followups(hass)) == 1 and ("wake", "cancel_close_wakes") in hass.log,
          followups(hass))
    check("fix1: ends locked (device's word)", ctl._state == "locked")


def t_hold_then_live_lock_credential():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t_fp = CLOCK.now
    CLOCK.advance(2)
    door(hass, ctl, "on")
    CLOCK.advance(3)
    raw(ctl, type="unlock", dt=t_fp, method="fingerprint", cred=1)  # flushed
    CLOCK.advance(3)
    door(hass, ctl, "off")
    CLOCK.advance(2)
    raw(ctl, type="state_report", dt=CLOCK.now, locked=True)
    CLOCK.advance(20)
    rows = [r[0] for r in activity_rows(hass)]
    check("fix1: hold w/ collected fp + live lock -> fp line THEN lock line",
          rows == ["Unlocked by Luke (fingerprint)", "Device locked"], rows)


def t_hold_buffered_prior_lock_keeps_holding():
    """A buffered relock from BEFORE the open must not resolve the hold early:
    a later credential in the same flush can still explain the open."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t0 = CLOCK.now
    CLOCK.advance(40)
    door(hass, ctl, "on")
    CLOCK.advance(3)
    raw(ctl, type="unlock", dt=t0, fresh=False, method="card", cred=51)
    raw(ctl, type="state_report", dt=t0 + 10, fresh=False, locked=True)
    raw(ctl, type="unlock", dt=t0 + 38, fresh=False, method="fingerprint", cred=1)
    CLOCK.advance(controller_mod.DOOR_HOLD_S)
    rows = [r[0] for r in activity_rows(hass)]
    check("fix1: prior card, prior relock, then fp explaining the open (device order)",
          rows == ["Unlocked by Spare card (card)", "Device locked",
                   "Unlocked by Luke (fingerprint)"], rows)


def t_hold_lock_within_slack_after_close():
    """Door closed during the hold; a lock reported slightly BEFORE the open by
    device time (clock skew) is treated as the post-close lock-up."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t_open = CLOCK.now
    door(hass, ctl, "on")
    CLOCK.advance(4)
    door(hass, ctl, "off")
    CLOCK.advance(2)
    raw(ctl, type="state_report", dt=t_open - 2, locked=True)
    CLOCK.advance(20)
    rows = [r[0] for r in activity_rows(hass)]
    check("fix1: skewed lock after close still resolves the hold first",
          rows == ["Unlocked (thumbturn)", "Device locked"], rows)


# ====================================================== fix 2: exit re-sync only

def t_exit_resync_close_before_resolve():
    """v0.1.3 (replaces the +60s exit re-sync): a close while a hold is pending
    starts the post-close series AT the close."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "on")
    CLOCK.advance(6)
    t_close = CLOCK.now
    door(hass, ctl, "off")
    fu = followups(hass)
    check("close series: close during a hold -> series started at the close",
          len(fu) == 1 and fu[0][2] == t_close, fu)
    CLOCK.advance(controller_mod.DOOR_HOLD_S)  # resolves -> thumbturn
    check("close series: the hold resolving adds no second series",
          len(followups(hass)) == 1, followups(hass))



def t_exit_resync_close_after_resolve():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "on")
    CLOCK.advance(controller_mod.DOOR_HOLD_S + 2)  # thumbturn, door still open
    check("close series: thumbturn resolved, door open -> not yet", not followups(hass))
    door(hass, ctl, "off")
    check("close series: close while UNLOCKED -> series", len(followups(hass)) == 1,
          followups(hass))
    n_cancel = hass.log.count(("wake", "cancel_close_wakes"))
    CLOCK.advance(5)
    door(hass, ctl, "on")
    check("close series: a reopen cancels the rest of the series",
          hass.log.count(("wake", "cancel_close_wakes")) == n_cancel + 1)
    door(hass, ctl, "off")
    check("close series: ...and the next close starts a new one",
          len(followups(hass)) == 2, followups(hass))



def t_exit_resync_not_for_credential_close_before():
    """v0.1.3: entries (credential) get the post-close series too (was: exits
    only)."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t_fp = CLOCK.now
    CLOCK.advance(2)
    door(hass, ctl, "on")
    CLOCK.advance(3)
    raw(ctl, type="unlock", dt=t_fp, method="card", cred=51)
    CLOCK.advance(2)
    door(hass, ctl, "off")
    CLOCK.advance(controller_mod.DOOR_HOLD_S)
    rows = [r[0] for r in activity_rows(hass)]
    check("close series: card entry -> card line, and its close starts the series",
          rows == ["Unlocked by Spare card (card)"] and len(followups(hass)) == 1,
          (rows, followups(hass)))
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "off", old="on")
    check("close series: a close while LOCKED (no hold) starts nothing",
          not followups(hass), followups(hass))
    hass, ctl = make()
    door(hass, ctl, "off", old="on")
    check("close series: a close from UNKNOWN starts it",
          len(followups(hass)) == 1, followups(hass))



def t_fresh_lock_cancels_followup():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    raw(ctl, type="state_report", dt=CLOCK.now, locked=True)
    # v0.1.2 (H3): a live lock's side effects wait for the debounce to settle
    check("fix2: ...not before the lock settles (bounce guard)",
          ("wake", "cancel_close_wakes") not in hass.log)
    CLOCK.advance(controller_mod.DEVICE_DEBOUNCE_S + 0.1)
    check("close series: a settled device lock cancels the rest of the series",
          ("wake", "cancel_close_wakes") in hass.log)


# ======================================================= fix 3: wake coalescing
class FakeListener:
    def __init__(self):
        self.wakes = []

    def wake(self):
        self.wakes.append(CLOCK.now)


def make_wake():
    CLOCK.timers.clear()
    lst = FakeListener()
    hass = types.SimpleNamespace(async_add_executor_job=lambda f: f())
    return wake_mod.EraWakeManager(hass, lst, heartbeat_s=0), lst



def t_wake_coalesce():
    wm, lst = make_wake()
    # A door close at T starts +10/+20/+30; an HA unlock at T+0.5 arms the
    # relock polls at +30.5/+40.5/+50.5. The +30.5 poll merges into the +30
    # close wake (0.5s apart, inside the coalescing window).
    t0 = CLOCK.now
    wm.schedule_close_wakes()
    CLOCK.advance(0.5)
    wm.on_unlock()
    CLOCK.advance(60)
    got = [round(w - t0, 1) for w in lst.wakes]
    check("close series + relock polls: 10/20/30 then 40.5/50.5 (the +30.5 poll merged)",
          got == [10.0, 20.0, 30.0, 40.5, 50.5], got)


def t_wake_coalesce_boundary():
    wm, lst = make_wake()
    wm.on_door_change(force=True)
    CLOCK.advance(wake_mod.FORCED_WAKE_COALESCE_S + 0.1)
    wm.on_door_change(force=True)
    check("fix3: forced wakes > window apart both go", len(lst.wakes) == 2, lst.wakes)
    CLOCK.advance(1)
    wm.request_wake("manual")
    check("fix3: non-forced still bound by MIN_WAKE_GAP", len(lst.wakes) == 2, lst.wakes)
    CLOCK.advance(wake_mod.MIN_WAKE_GAP)
    wm.request_wake("manual")
    CLOCK.advance(2)
    wm.on_door_change(force=True)
    check("fix3: forced wake 2s after a non-forced one is merged",
          len(lst.wakes) == 3, lst.wakes)


def t_polls_unchanged():
    wm, lst = make_wake()
    wm.on_unlock()
    CLOCK.advance(60)
    check("fix3: post-unlock polls alone -> 3 wakes at +30/+40/+50",
          len(lst.wakes) == 3, lst.wakes)


# ===================================================== fix 4: offline / online
def make_listener(lock_id="LOCKDEV"):
    lst = object.__new__(listener_mod.EraMqttListener)
    lst._helper = types.SimpleNamespace(cid="CID1")
    lst._roster = {}
    lst._lock_dev_id = lock_id
    lst._wake_manager = None
    lst.stats = __import__("collections").defaultdict(int)
    out = []
    lst._emit = out.append
    return lst, out


def t_listener_link_frames():
    lst, out = make_listener()
    lst._handle({"protocol": 25, "t": 1, "data": {"online": [], "offline": ["CID1"]}})
    lst._handle({"protocol": 25, "t": 2, "data": {"online": ["CID1"], "offline": [],
                                                   "nearby": ["CID1"]}})
    lst._handle({"protocol": 25, "t": 3, "data": {"online": ["OTHER"], "offline": []}})
    lst._handle({"protocol": 1, "t": 4, "data": {"devId": "LOCKDEV", "connectStatus": 0,
                                                  "online": False}})
    lst._handle({"protocol": 1, "t": 5, "data": {"devId": "LOCKDEV", "connectStatus": 2,
                                                  "online": True}})
    lst._handle({"protocol": 1, "t": 6, "data": {"devId": "LOCKDEV", "connectStatus": 1,
                                                  "online": True}})
    lst._handle({"protocol": 1, "t": 7, "data": {"devId": "HUB", "connectStatus": 0,
                                                  "online": False}})
    lst._handle({"protocol": 25, "t": 8, "data": {"online": [], "offline": [],
                                                   "nearby": ["CID1"]}})
    got = [(e.type, e.value, e.dt) for e in out]
    check("fix4: listener normalises protocol:25 / protocol:1 link frames "
          "(nearby + connectStatus 2 = online; other ids ignored)",
          got == [("connectivity", False, 1), ("connectivity", True, 2),
                  ("connectivity", False, 4), ("connectivity", True, 5),
                  ("connectivity", True, 6), ("connectivity", True, 8)], got)
    lst2, out2 = make_listener(lock_id=None)
    lst2._handle({"protocol": 1, "t": 4, "data": {"devId": "X", "connectStatus": 0}})
    check("fix4: protocol:1 ignored without a configured lock_id", not out2, out2)
    lst3, out3 = make_listener()
    lst3._handle({"protocol": 4, "t": 100, "data": {"cid": "CID1", "dps": {"11": True},
                                                     "t": {"11": 100}}})
    check("fix4: dps frames still parsed", [e.type for e in out3] == ["state_report"],
          out3)


def t_offline_online():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    ctl._inferred = False
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)  # p1 dup
    rows = activity_rows(hass)
    last = hass.states_log[-1]
    check("fix4: offline -> one 'Lock offline (state unknown)' line",
          [r[0] for r in rows] == ["Lock offline (state unknown)"], rows)
    check("fix4: offline -> state publish unavailable, parented to that line",
          last["available"] is False and last["cause_ctx_id"] == rows[0][2], last)
    ee = [r for r in hass.log if r[0] == "event_entity"]
    check("fix4: event entity fires 'connectivity'", ee and ee[-1][1] == "connectivity", ee)
    CLOCK.advance(30)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)
    rows = activity_rows(hass)
    last = hass.states_log[-1]
    check("fix4: back online -> 'Lock back online' line, available, parented",
          [r[0] for r in rows][-1] == "Lock back online" and last["available"] is True
          and last["cause_ctx_id"] == rows[-1][2], (rows, last))
    check("link: back online -> state UNKNOWN (not the last known), inferred",
          last["state"] is None and last["inferred"] is True
          and last["changed_by"] == "Lock back online (state unknown)", last)
    check("fix4: back online forces a sync", ("wake", "on_reconnect") in hass.log)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)  # nearby etc.
    check("fix4: repeated online reports add no lines",
          len(activity_rows(hass)) == 2, activity_rows(hass))


def t_offline_implicit_online():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    CLOCK.advance(10)
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    CLOCK.advance(3)
    rows = [r[0] for r in activity_rows(hass)]
    last = hass.states_log[-1]
    check("fix4: a device DP frame while 'offline' = back online, then processed",
          rows == ["Lock offline (state unknown)", "Lock back online",
                   "Unlocked by Luke (fingerprint)"]
          and last["available"] is True and last["state"] == "unlocked", (rows, last))


# ============================================ fix 5: command rows -> activity ctx
class Auth:
    async def async_get_user(self, uid):
        return types.SimpleNamespace(name="Julie")


def make_lock_entity(hass, ctl):
    coord = ctl.coordinator
    coord.controller = ctl
    ent = lock_mod.EraLockEntity(coord, types.SimpleNamespace(entry_id="E1",
                                                              unique_id="HUB"))
    ent.hass = hass
    ent.rows = []
    hass.auth = Auth()
    async_dispatcher_connect(hass, events.signal_state("E1"), ent._on_state)
    return ent


def run_unlock(hass, ctl, ent, *, refresh_s=0.0, fail=False):
    import asyncio

    async def go():
        def job(f):
            if fail:
                raise HomeAssistantError("Unlock command sent but not confirmed")
            return asyncio.sleep(0)

        async def refresh():
            CLOCK.advance(refresh_s)  # e.g. a slow local LAN poll
        hass.async_add_executor_job = job
        ctl.coordinator.async_request_refresh = refresh
        # HA sets the service-call context on the entity before the call.
        ent.async_set_context(Context(user_id="uid-julie"))
        try:
            await ent.async_unlock()
        except HomeAssistantError:
            pass
    asyncio.run(go())


def visible_rows(ent):
    """Rows whose state differs from the previous row (logbook rows)."""
    out, prev = [], object()
    for st, ctx, t, attrs in ent.rows:
        if st != prev:
            out.append((st, ctx))
        prev = st
    return out


def t_unlock_while_unlocked_links():
    hass, ctl = make()
    ent = make_lock_entity(hass, ctl)
    ctl._state = ctl._logged_state = "unlocked"
    ent._state = "unlocked"
    run_unlock(hass, ctl, ent)
    rows = activity_rows(hass)
    vis = visible_rows(ent)
    check("fix5: HA unlock while already unlocked still emits its line",
          [r[0] for r in rows] == ["Unlocked in Home Assistant"], rows)
    check("fix5: ... and the spinner's final 'unlocked' row parents to it (By Julie)",
          [v[0] for v in vis] == ["unlocking", "unlocked"]
          and vis[1][1].parent_id == rows[0][2] and vis[1][1].user_id == "uid-julie",
          [(v[0], v[1].parent_id, v[1].user_id) for v in vis])
    check("fix5: the 'unlocking' row keeps the service-call context (no activity yet)",
          vis[0][1].parent_id is None and vis[0][1].user_id == "uid-julie")


def t_unlock_slow_refresh_links():
    hass, ctl = make()
    ent = make_lock_entity(hass, ctl)
    ctl._state = ctl._logged_state = "locked"
    ent._state = "locked"
    run_unlock(hass, ctl, ent, refresh_s=8.0)  # > HA's 5s context window
    rows = activity_rows(hass)
    vis = visible_rows(ent)
    check("fix5: normal unlock, final 'unlocked' row parented even after >5s",
          [v[0] for v in vis] == ["unlocking", "unlocked"]
          and vis[-1][1].parent_id == rows[0][2], [(v[0], v[1].parent_id) for v in vis])


def t_unlock_failure_no_link():
    hass, ctl = make()
    ent = make_lock_entity(hass, ctl)
    ctl._state = ctl._logged_state = "locked"
    ent._state = "locked"
    run_unlock(hass, ctl, ent, fail=True)
    vis = visible_rows(ent)
    check("fix5: failed unlock -> no activity line, revert row not parented",
          not activity_rows(hass) and [v[0] for v in vis] == ["unlocking", "locked"]
          and vis[-1][1].parent_id is None, [(v[0], v[1].parent_id) for v in vis])


def t_repeat_state_no_new_ctx():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    ctl._last_device_dt = 0
    raw(ctl, type="state_report", dt=CLOCK.now, fresh=False, locked=False)
    CLOCK.advance(3)
    n = len(state_changes(hass))
    ctl._transition("unlocked", "again")  # no activity, same logged state
    check("fix5: a repeat publish with no emitted activity mints no context",
          hass.states_log[-1]["cause_ctx_id"] is None and len(state_changes(hass)) == n,
          hass.states_log[-1])


def t_lock_entity_unavailable():
    hass, ctl = make()
    ent = make_lock_entity(hass, ctl)
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    ent.async_write_ha_state()
    off_row = ent.rows[-2]
    check("fix4: lock entity unavailable while offline, row parented to 'Lock offline'",
          ent.available is False and off_row[0] == "unavailable"
          and off_row[1].parent_id == activity_rows(hass)[0][2],
          (ent.available, off_row))
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)
    check("link: lock entity available + UNKNOWN when back online, parented",
          ent.available is True and ent.rows[-1][0] is None
          and ent.is_locked is None
          and ent.rows[-1][1].parent_id == activity_rows(hass)[-1][2], ent.rows[-1])


# ========================================== link: unknown after reconnect + sensor
def offline_online(ctl, gap=30):
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    CLOCK.advance(gap)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)


def t_reconnect_unknown_then_ha_unlock():
    hass, ctl = make()
    ent = make_lock_entity(hass, ctl)
    ctl._state = ctl._logged_state = "locked"
    ent._state = "locked"
    offline_online(ctl)
    check("link: after reconnect the lock entity reads unknown",
          ent.is_locked is None and ent.available, ent.rows[-1])
    run_unlock(hass, ctl, ent)
    rows = [r[0] for r in activity_rows(hass)]
    check("link: an HA unlock after reconnect resolves unknown -> unlocked",
          ent.is_locked is False and ctl._state == "unlocked"
          and rows[-1] == "Unlocked in Home Assistant"
          and ent.rows[-1][1].parent_id == activity_rows(hass)[-1][2],
          (rows, ent.rows[-1]))


def t_reconnect_unknown_then_device_dp():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    offline_online(ctl)
    CLOCK.advance(20)
    raw(ctl, type="state_report", dt=CLOCK.now, locked=True)
    CLOCK.advance(3)
    last = hass.states_log[-1]
    check("link: a device dp11 lock after reconnect resolves unknown -> locked",
          last["state"] == "locked" and last["inferred"] is False
          and last["changed_by"] == "Device locked"
          and last["cause_ctx_id"] == activity_rows(hass)[-1][2], last)
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    offline_online(ctl)
    CLOCK.advance(5)
    raw(ctl, type="state_report", dt=CLOCK.now, fresh=False, locked=True)
    CLOCK.advance(3)
    check("link: a synced dp11 equal to the pre-outage state still resolves unknown",
          hass.states_log[-1]["state"] == "locked"
          and hass.states_log[-1]["changed_by"] == "Synced from lock: locked",
          hass.states_log[-1])


def t_reconnect_unknown_then_door():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    offline_online(ctl)
    door(hass, ctl, "on")
    check("link: a door open after reconnect resolves unknown -> unlocked",
          ctl._state == "unlocked" and hass.states_log[-1]["state"] == "unlocked",
          hass.states_log[-1])


def t_reconnect_via_dp11_sets_state():
    for locked in (True, False):
        hass, ctl = make()
        ent = make_lock_entity(hass, ctl)
        ctl._state = ctl._logged_state = "unlocked" if locked else "locked"
        raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
        CLOCK.advance(30)
        raw(ctl, type="state_report", dt=CLOCK.now, locked=locked)  # implicit online
        CLOCK.advance(3)
        rows = [r[0] for r in activity_rows(hass)]
        want = "locked" if locked else "unlocked"
        seq = [s["state"] for s in hass.states_log]
        check(f"link: reconnect via a dp11={'locked' if locked else 'unlocked'} "
              f"frame -> online, unknown, then that DP's state (not unknown)",
              rows[:2] == ["Lock offline (state unknown)", "Lock back online"]
              and seq[-2:] == [None, want] and ctl._state == want
              and ent.available and ent.rows[-1][0] == want, (rows, seq))
        CLOCK.advance(120)
        check(f"link: ... and stays {want} (nothing re-imposes after)",
              ctl._state == want and hass.states_log[-1]["state"] == want,
              hass.states_log[-1])


def t_reconnect_no_stale_relock():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    CLOCK.advance(3)   # applied: unlocked, relock armed for 60s
    check("link: (setup) unlocked with relock armed",
          ctl._state == "unlocked" and ctl._relock_cancel is not None)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    CLOCK.advance(10)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)
    n = len(activity_rows(hass))
    CLOCK.advance(300)
    check("link: the pre-outage relock failsafe never fires (no 'Auto-locked' "
          "line, state stays unknown)",
          ctl._state is None and len(activity_rows(hass)) == n
          and not any(r[0].startswith("Auto-locked") for r in activity_rows(hass)),
          activity_rows(hass))
    # offline for longer than the relock window: no auto-lock while offline either
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    CLOCK.advance(3)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    CLOCK.advance(300)
    check("link: no timer auto-lock while offline",
          not any(r[0].startswith("Auto-locked") for r in activity_rows(hass)),
          activity_rows(hass))


def t_reconnect_no_stale_debounce():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    raw(ctl, type="state_report", dt=CLOCK.now, locked=False)
    CLOCK.advance(0.5)  # still within DEVICE_DEBOUNCE_S
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    seq = [(s["state"], s["available"]) for s in hass.states_log]
    check("link: a pending debounced state is flushed BEFORE the outage row",
          seq[-2:] == [("unlocked", True), ("unlocked", False)], seq)
    CLOCK.advance(0.5)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)
    CLOCK.advance(30)
    check("link: ... and nothing re-applies it after the reconnect",
          ctl._state is None and hass.states_log[-1]["state"] is None
          and ctl._device_debounce is None and ctl._pending is None,
          hass.states_log[-1])


def t_reconnect_hold_no_state():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    door(hass, ctl, "on")   # door opened from locked: hold pending
    check("link: (setup) hold pending", ctl._door_hold is not None)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    CLOCK.advance(2)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)
    n_states = len(hass.states_log)
    CLOCK.advance(30)  # hold resolves (thumbturn)
    rows = [r[0] for r in activity_rows(hass)]
    check("link: a door hold resolving after reconnect emits its line only "
          "(state stays unknown)",
          rows[-1] == "Unlocked (thumbturn)" and ctl._state is None
          and all(s["state"] is None for s in hass.states_log[n_states:]),
          (rows, hass.states_log[n_states:]))


def make_link_sensor(hass, ctl):
    coord = ctl.coordinator
    coord.controller = ctl
    ent = bs_mod.EraLockConnectedSensor(coord, types.SimpleNamespace(
        entry_id="E1", unique_id="HUB"))
    ent.hass = hass
    ent.rows = []
    async_dispatcher_connect(hass, events.signal_link("E1"), ent._on_link)
    return ent


def t_link_sensor():
    hass, ctl = make()
    lock_ent = make_lock_entity(hass, ctl)
    sen = make_link_sensor(hass, ctl)
    check("sensor: unique id, diagnostic connectivity, has_entity_name",
          sen._attr_unique_id == "HUB_connected"
          and sen._attr_device_class == "connectivity"
          and sen._attr_entity_category == "diagnostic" and sen._attr_has_entity_name
          and sen._attr_translation_key == "connected"
          and sen._attr_device_info == lock_ent._attr_device_info,
          (sen._attr_unique_id, sen._attr_device_class, sen._attr_entity_category))
    check("sensor: on at startup (controller assumes online), available",
          sen.is_on is True and sen.available is True)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)  # dup
    rows = activity_rows(hass)
    check("sensor: offline -> off, still available while the lock is unavailable",
          sen.is_on is False and sen.available is True
          and lock_ent.available is False and sen.rows == sen.rows[:1]
          and sen.rows[-1][0] == "off"
          and sen.rows[-1][1].parent_id == rows[-1][2], sen.rows)
    CLOCK.advance(10)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)
    check("sensor: back online -> on, parented to 'Lock back online'",
          sen.is_on is True and len(sen.rows) == 2 and sen.rows[-1][0] == "on"
          and sen.rows[-1][1].parent_id == activity_rows(hass)[-1][2], sen.rows)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    CLOCK.advance(10)
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    check("sensor: a device DP frame while offline turns it back on",
          sen.is_on is True and [r[0] for r in sen.rows] == ["off", "on", "off", "on"],
          sen.rows)
    # starts from the controller's current view
    hass, ctl = make()
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    sen2 = make_link_sensor(hass, ctl)
    check("sensor: initial value mirrors the controller's current view",
          sen2.is_on is False and sen2.available is True)



# ====================================== MQTT: unlock rides the listener's client
COMBINED = {"lock_cid": "CID1", "hub_id": "HUB", "local_key": "0123456789abcdef"}


class FakeMgr:
    def __init__(self):
        self.refreshes = 0

    def mqtt_secrets(self):
        return {"broker": "ssl://m1.example:8883", "clientId": "FIXED-CID",
                "username": "user", "password": f"pw{self.refreshes}"}

    def refresh_tuya_session(self):
        self.refreshes += 1


def echo_responder(client, dps):
    """The lock: wakes + reports on dp54; echoes dp61 AEkA (+ dp11 false)."""
    if dps == {"54": "Aw=="}:
        return [(0.05, {"protocol": 4, "t": int(CLOCK.now),
                        "data": {"cid": "CID1", "dps": {"8": 90}}})]
    if dps == {"61": "AQ=="}:
        return [(0.05, {"protocol": 4, "t": int(CLOCK.now),
                        "data": {"cid": "CID1", "dps": {"61": "AEkA", "11": False},
                                 "t": {"61": int(CLOCK.now), "11": int(CLOCK.now)}}})]
    return []


def wake_only_responder(client, dps):
    return echo_responder(client, dps) if dps == {"54": "Aw=="} else []


def make_mqtt_listener(*, connected=True, responder=echo_responder):
    FAKE_CLIENTS.clear()
    queued = []  # (fn, args) bridged to the HA loop, drained on the main thread
    hass = types.SimpleNamespace(loop=types.SimpleNamespace(
        call_soon_threadsafe=lambda f, *a: queued.append((f, a))))
    mgr = FakeMgr()
    lst = listener_mod.EraMqttListener(hass, mgr, dict(COMBINED), "E1")
    lst.start()
    client = lst._client
    client.responder = responder
    if connected:
        lst._on_connect(client, None, {}, 0, None)
    return lst, client, mgr, queued


def fast(lst, **kw):
    kw.setdefault("online_wait", 0.3)
    kw.setdefault("echo_wait", 0.3)
    return lst.unlock(**kw)


def dps_sent(client):
    return [f["data"]["dps"] for _, f in client.published]


def t_mqtt_unlock_on_listener():
    lst, client, mgr, queued = make_mqtt_listener()
    t0 = _rtime.monotonic()
    ok = fast(lst)
    took = _rtime.monotonic() - t0
    check("mqtt: unlock confirmed via the listener's dp61 echo", ok is True)
    check("mqtt: no second MQTT client created (one client, the listener's)",
          len(FAKE_CLIENTS) == 1 and FAKE_CLIENTS[0] is client, FAKE_CLIENTS)
    check("mqtt: wake then unlock published on the listener's client, out topic",
          dps_sent(client) == [{"54": "Aw=="}, {"61": "AQ=="}]
          and {t for t, _ in client.published} == {"smart/mb/out/HUB"}
          and all(f["data"]["cid"] == "CID1" for _, f in client.published),
          client.published)
    check("mqtt: confirmed promptly (no settle sleep on the persistent link)",
          took < 0.5, took)
    check("mqtt: tracker deregistered after the unlock", not lst._trackers)


def t_mqtt_frames_still_delivered():
    lst, client, mgr, queued = make_mqtt_listener()
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    assert fast(lst)
    _rtime.sleep(0.05)
    for f, a in list(queued):   # bridge to the "HA loop": controller dispatch
        if f is listener_mod.async_dispatcher_send:
            f(hass, *a[1:])
        else:
            f(*a)
    CLOCK.advance(3)
    evs = [a[2] for f, a in queued]
    check("v0.1.3: the wake's DP dump (dp8) is dispatched as a battery reading",
          [(e.type, e.value) for e in evs if e.type == "battery"] == [("battery", 90)],
          evs)
    evs = [e for e in evs if e.type != "battery"]
    check("mqtt: frames seen during an unlock still reach the dispatcher "
          "(state_report + remote unlock from the echo)",
          [e.type for e in evs] == ["state_report", "unlock"]
          and evs[1].method == "remote" and evs[0].locked is False,
          [(e.type, e.method) for e in evs])
    check("mqtt: ...and the controller applies them",
          ctl._state == "unlocked", ctl._state)


def t_mqtt_unlock_no_echo():
    lst, client, mgr, queued = make_mqtt_listener(responder=wake_only_responder)
    ok = fast(lst, echo_wait=0.1)
    check("mqtt: no dp61 echo -> False after 3 unlock attempts (same protocol)",
          ok is False and dps_sent(client) == [{"54": "Aw=="}] + [{"61": "AQ=="}] * 3,
          dps_sent(client))
    # controller surfaces it as HomeAssistantError
    hass, ctl = make()
    ctl.coordinator.listener = types.SimpleNamespace(
        unlock=lambda: fast(lst, echo_wait=0.05), refresh_session=lst.refresh_session)
    try:
        ctl._unlock()
        raised = None
    except HomeAssistantError as e:
        raised = str(e)
    check("mqtt: unconfirmed unlock -> HomeAssistantError (no session refresh)",
          raised and "not confirmed" in raised and mgr.refreshes == 0, raised)


def t_mqtt_unlock_waits_for_connection():
    lst, client, mgr, queued = make_mqtt_listener(connected=False)
    t0 = _rtime.monotonic()
    try:
        fast(lst, connect_wait=0.3)
        err = None
    except MqttUnlockError as e:
        err = str(e)
    took = _rtime.monotonic() - t0
    check("mqtt: listener disconnected -> waits connect_wait, then MqttUnlockError, "
          "nothing published", err and "not connected" in err and 0.25 < took < 1.0
          and not client.published, (err, took))
    # reconnects while the unlock is waiting -> proceeds on the same client
    _threading.Timer(0.15, lambda: lst._on_connect(client, None, {}, 0, None)).start()
    ok = fast(lst, connect_wait=2.0)
    check("mqtt: listener reconnects within the wait -> unlock proceeds",
          ok is True and len(FAKE_CLIENTS) == 1, dps_sent(client))
    # drop mid-unlock: after the wake, the link goes down -> no dp61 handed to paho
    lst, client, mgr, queued = make_mqtt_listener()
    client.responder = lambda c, dps: (lst._on_disconnect(c, None, None, 0, None) or [])
    try:
        fast(lst)
        err = None
    except MqttUnlockError as e:
        err = str(e)
    check("mqtt: disconnect mid-unlock -> MqttUnlockError, unlock frame never queued",
          err and "mid-unlock" in err and dps_sent(client) == [{"54": "Aw=="}],
          (err, dps_sent(client)))


def t_mqtt_drop_unsent():
    lst, client, mgr, queued = make_mqtt_listener()
    client.publish_rc = 4  # paho NO_CONN: the frame is QUEUED for the reconnect
    try:
        fast(lst)
    except MqttUnlockError:
        pass
    check("mqtt: a frame paho queued (NO_CONN) is dropped, never replayed on reconnect",
          client.published and not client._out_messages, client._out_messages)
    lst, client, mgr, queued = make_mqtt_listener(responder=wake_only_responder)
    client.publish_unacked = True
    fast(lst, echo_wait=0.05)
    check("mqtt: un-PUBACKed unlock frames dropped after the unlock (inflight fixed)",
          not client._out_messages and client._inflight_messages == 0,
          (client._out_messages, client._inflight_messages))


def t_mqtt_refresh_applied_to_listener():
    lst, client, mgr, queued = make_mqtt_listener(connected=False)
    # paho's reconnect loop: once fresh creds are set, the next CONNECT succeeds
    client.on_creds = lambda c, lst=lst: (
        c.creds[1] != "pw0" and _threading.Timer(
            0.1, lambda: lst._on_connect(c, None, {}, 0, None)).start())
    hass, ctl = make()
    ctl.coordinator.listener = types.SimpleNamespace(
        unlock=lambda: fast(lst, connect_wait=0.3), refresh_session=lst.refresh_session)
    try:
        ctl._unlock()
        raised = None
    except HomeAssistantError as e:
        raised = e
    check("mqtt: MqttUnlockError -> session re-minted once, applied to the "
          "LISTENER's client, retried on it",
          raised is None and mgr.refreshes == 1 and client.creds == ("user", "pw1")
          and len(FAKE_CLIENTS) == 1
          and dps_sent(client) == [{"54": "Aw=="}, {"61": "AQ=="}],
          (raised, mgr.refreshes, client.creds, len(FAKE_CLIENTS)))
    # the paho thread's own auth-failure refresh right after reuses that session
    lst._on_connect(client, None, {}, 5, None)
    check("mqtt: paho-thread auth refresh within 30s reuses it (no 2nd re-mint), "
          "still reconnects", mgr.refreshes == 1 and client.reconnects == 1
          and not lst.connected, (mgr.refreshes, client.reconnects))
    _rtime.sleep(0.15)
    # still down after the refresh -> clean HomeAssistantError
    lst, client, mgr, queued = make_mqtt_listener(connected=False)
    hass, ctl = make()
    ctl.coordinator.listener = types.SimpleNamespace(
        unlock=lambda: fast(lst, connect_wait=0.1), refresh_session=lst.refresh_session)
    try:
        ctl._unlock()
        raised = None
    except HomeAssistantError as e:
        raised = str(e)
    check("mqtt: still disconnected after refresh -> HomeAssistantError",
          raised and raised.startswith("Unlock failed:") and mgr.refreshes == 1
          and not client.published, (raised, mgr.refreshes, client.published))


def t_mqtt_standalone_cloud_lock():
    FAKE_CLIENTS.clear()
    lock = mqtt_mod.CloudMqttLock(dict(COMBINED), FakeMgr().mqtt_secrets())
    orig = FakeClient.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.responder = echo_responder
    FakeClient.__init__ = init
    try:
        seen = []
        ok = lock.unlock(online_wait=0.5, on_event=lambda t, js: seen.append(js))
    finally:
        FakeClient.__init__ = orig
    check("mqtt: standalone CloudMqttLock.unlock (CLI) unchanged: own client, "
          "wake+unlock, confirmed, on_event called",
          ok is True and len(FAKE_CLIENTS) == 1
          and dps_sent(FAKE_CLIENTS[0]) == [{"54": "Aw=="}, {"61": "AQ=="}]
          and len(seen) == 2, (ok, len(FAKE_CLIENTS), seen))


def t_mqtt_tracker():
    tr = mqtt_mod.UnlockTracker("CID1")
    tr.feed('{"protocol":4,"data":{"cid":"OTHER","dps":{"61":"AEkA"}}}')
    tr.feed(None)
    tr.feed("not json")
    tr.feed({"protocol": 25, "data": ["x"]})
    check("tracker: other cid / junk ignored", not tr.online and not tr.unlocked)
    tr.feed({"protocol": 25, "data": {"online": ["CID1"]}})
    check("tracker: p25 online listing -> online only", tr.online and not tr.unlocked)
    tr.feed({"protocol": 4, "data": {"cid": "CID1", "dps": {"61": "AEkA"}}})
    check("tracker: dp61 AEkA from our cid -> unlocked", tr.unlocked)
    tr2 = mqtt_mod.UnlockTracker("CID1")
    t0 = _rtime.monotonic()
    got = tr2.wait("unlocked", 0.1)
    check("tracker: wait times out False", got is False
          and _rtime.monotonic() - t0 >= 0.09)


# ------------------------------------------- HA unlock: absorb our own echo --
def f61(ctl, dt=None):   # live {"dps":{"61":"AEkA"}} (no data.t -> dt=frame_t)
    raw(ctl, type="unlock", dt=int(CLOCK.now) if dt is None else dt, method="remote")


def f62(ctl, dt=None):   # live {"dps":{"62":0}}
    raw(ctl, type="unlock", dt=int(CLOCK.now) if dt is None else dt,
        method="phone_remote", cred=0)


def f11(ctl):            # live {"dps":{"11":false}}
    raw(ctl, type="state_report", dt=int(CLOCK.now), locked=False)


def ha_unlock(hass, ctl, ent=None, *, during=None, fail=False):
    """Drive an HA unlock; ``during`` runs while the command is in flight
    (frames the listener hears before drive_unlock returns)."""
    import asyncio
    out = {}

    async def go():
        def job(f):
            if during:
                during()
            if fail:
                raise HomeAssistantError("Unlock command sent but not confirmed")
            return asyncio.sleep(0)
        hass.async_add_executor_job = job
        ctl.coordinator.async_request_refresh = lambda: asyncio.sleep(0)
        try:
            if ent is not None:
                ent.async_set_context(Context(user_id="uid-julie"))
                await ent.async_unlock()
            else:
                out["ctx"] = await ctl.async_unlock("uid-julie", "Julie")
        except HomeAssistantError:
            out["failed"] = True
    asyncio.run(go())
    return out


def _one_ha_line(name, hass, ctl, extra=""):
    rows = activity_rows(hass)
    sc = state_changes(hass)
    check(f"echo: {name}: exactly ONE line 'Unlocked in Home Assistant' by Julie{extra}",
          [(r[0], r[1]) for r in rows] == [("Unlocked in Home Assistant", "uid-julie")],
          rows)
    check(f"echo: {name}: one unlocked state row, parented to it, By Julie, "
          "reason not re-attributed",
          len(sc) == 1 and sc[0]["state"] == "unlocked" and rows
          and sc[0]["cause_ctx_id"] == rows[0][2] and sc[0]["user_id"] == "uid-julie"
          and hass.states_log[-1]["changed_by"] == "Unlocked in Home Assistant"
          and hass.states_log[-1]["last_method"] == "remote"
          and hass.states_log[-1]["last_user"] == "Julie"
          and ctl._state == "unlocked", (sc, hass.states_log[-1:]))


def t_echo_dp61_before_return():
    """The live capture: dp61 before the command returns, dp62 + dp11 ~100ms after."""
    hass, ctl = make()
    ent = make_lock_entity(hass, ctl)
    ctl._state = ctl._logged_state = ent._state = "locked"

    def during():
        CLOCK.advance(4.5)
        f61(ctl)
    ha_unlock(hass, ctl, ent, during=during)
    CLOCK.advance(0.1); f62(ctl); f11(ctl)
    CLOCK.advance(10)
    _one_ha_line("dp61 before return + dp62/dp11 after", hass, ctl)
    rows = activity_rows(hass)
    vis = visible_rows(ent)
    check("echo: lock entity's final 'unlocked' row parents to the HA line",
          [v[0] for v in vis] == ["unlocking", "unlocked"]
          and vis[-1][1].parent_id == rows[0][2],
          [(v[0], v[1].parent_id) for v in vis])


def t_echo_dp62_before_dp61():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"

    def during():
        CLOCK.advance(1.0); f62(ctl)
        CLOCK.advance(0.1); f61(ctl)
    ha_unlock(hass, ctl, during=during)
    CLOCK.advance(0.05); f11(ctl)
    CLOCK.advance(10)
    _one_ha_line("dp62 before dp61 (both in flight)", hass, ctl)


def t_echo_both_after_return():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    ha_unlock(hass, ctl)
    CLOCK.advance(0.3); f61(ctl)
    CLOCK.advance(0.1); f62(ctl); f11(ctl)
    CLOCK.advance(10)
    _one_ha_line("dp61+dp62 both after return", hass, ctl)


def t_echo_3s_after_confirm():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    ha_unlock(hass, ctl)
    CLOCK.advance(3.0); f62(ctl)
    CLOCK.advance(0.1); f61(ctl); f11(ctl)
    CLOCK.advance(10)
    _one_ha_line("echo 3s after confirmation (inside grace)", hass, ctl)


def t_echo_debounce_applies_in_flight():
    """dp62 lands, the 2s debounce applies the state while the command is
    still waiting on dp61: still one HA line + one state row, By Julie."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"

    def during():
        CLOCK.advance(0.5); f62(ctl)
        CLOCK.advance(3.0)   # debounce fires in flight
        f61(ctl)
    ha_unlock(hass, ctl, during=during)
    CLOCK.advance(10)
    _one_ha_line("state applied by the echo while in flight", hass, ctl)


def t_echo_genuine_app_unlock_later():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"

    def during():
        CLOCK.advance(2); f61(ctl)
    ha_unlock(hass, ctl, during=during)
    CLOCK.advance(0.1); f62(ctl); f11(ctl)
    CLOCK.advance(30)
    raw(ctl, type="state_report", dt=int(CLOCK.now), locked=True)  # relocked
    CLOCK.advance(10)
    f61(ctl); CLOCK.advance(1); f62(ctl, dt=int(CLOCK.now) - 1)  # app unlock
    CLOCK.advance(10)
    rows = [(r[0], r[1]) for r in activity_rows(hass)]
    check("echo: genuine app unlock 40s after an HA unlock logs once as phone remote",
          rows == [("Unlocked in Home Assistant", "uid-julie"), ("Device locked", None),
                   ("Unlocked (phone remote)", None)], rows)
    sc = state_changes(hass)
    check("echo: ...and its state row is attributed to the phone remote",
          [s["state"] for s in sc] == ["unlocked", "locked", "unlocked"]
          and sc[-1]["changed_by"] == "Unlocked (phone remote)"
          and sc[-1]["cause_ctx_id"] == activity_rows(hass)[-1][2], sc)


def t_echo_window_boundary():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    ha_unlock(hass, ctl)
    CLOCK.advance(controller_mod.HA_ECHO_GRACE_S + 1)
    f61(ctl); CLOCK.advance(0.1); f62(ctl)
    CLOCK.advance(10)
    rows = [r[0] for r in activity_rows(hass)]
    check("echo: a remote unlock just past the grace window is NOT absorbed",
          rows == ["Unlocked in Home Assistant", "Unlocked (phone remote)"], rows)


def t_echo_fingerprint_not_swallowed():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"

    def during():
        CLOCK.advance(2); f61(ctl)
    ha_unlock(hass, ctl, during=during)
    CLOCK.advance(0.1); f62(ctl); f11(ctl)
    CLOCK.advance(1.0)
    raw(ctl, type="unlock", dt=int(CLOCK.now), method="fingerprint", cred=1)
    CLOCK.advance(10)
    rows = [(r[0], r[1]) for r in activity_rows(hass)]
    check("echo: a fingerprint unlock inside the window is still logged",
          rows == [("Unlocked in Home Assistant", "uid-julie"),
                   ("Unlocked by Luke (fingerprint)", "uid-luke")], rows)


def t_echo_buffered_replay_not_absorbed():
    """The command's wake (dp54) can flush buffered history: an OLD app unlock
    replayed in flight is a real earlier unlock, not our echo."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    old = CLOCK.now - 600

    def during():
        CLOCK.advance(1)
        raw(ctl, type="unlock", dt=old, fresh=False, method="phone_remote", cred=0)
        raw(ctl, type="state_report", dt=old + 30, fresh=False, locked=True)
        CLOCK.advance(1); f61(ctl)
    ha_unlock(hass, ctl, during=during)
    CLOCK.advance(0.1); f62(ctl)
    CLOCK.advance(10)
    rows = [r[0] for r in activity_rows(hass)]
    check("echo: buffered (stale) app unlock replayed in flight keeps its own line",
          rows == ["Unlocked (phone remote)", "Device locked",
                   "Unlocked in Home Assistant"], rows)


def t_echo_failed_then_late_echo():
    """Command reports unconfirmed, then the lock's response lands 2s later:
    our command DID unlock it — log it as the HA unlock (by Julie), once."""
    hass, ctl = make()
    ent = make_lock_entity(hass, ctl)
    ctl._state = ctl._logged_state = ent._state = "locked"

    def during():
        CLOCK.advance(12)  # attempts exhausted, nothing heard
    out = ha_unlock(hass, ctl, ent, during=during, fail=True)
    check("echo: unconfirmed with no echo -> the command fails, no line yet",
          not activity_rows(hass) and ent.is_locked is True, activity_rows(hass))
    CLOCK.advance(2); f61(ctl)
    CLOCK.advance(0.1); f62(ctl); f11(ctl)
    CLOCK.advance(10)
    _one_ha_line("late echo after an unconfirmed command", hass, ctl)
    check("echo: ...the lock entity follows to unlocked",
          ent.is_locked is False, ent.rows[-1])


def t_echo_failed_late_beyond_window():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    ha_unlock(hass, ctl, fail=True)
    CLOCK.advance(controller_mod.HA_ECHO_GRACE_S + 5)
    f61(ctl); CLOCK.advance(0.1); f62(ctl)
    CLOCK.advance(10)
    rows = [r[0] for r in activity_rows(hass)]
    check("echo: failed HA unlock, remote unlock well after the window -> phone remote",
          rows == ["Unlocked (phone remote)"], rows)


def t_echo_unconfirmed_but_reported():
    """dp62 heard in flight but the dp61 echo missed: the lock reported the
    unlock, so the command is treated as successful (no error, one line)."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"

    def during():
        CLOCK.advance(1); f62(ctl); f11(ctl)
        CLOCK.advance(11)
    out = ha_unlock(hass, ctl, during=during, fail=True)
    CLOCK.advance(10)
    check("echo: unconfirmed but reported by the lock -> no error raised",
          not out.get("failed") and out.get("ctx"), out)
    _one_ha_line("dp62-only in flight, tracker unconfirmed", hass, ctl)


def t_echo_door_hold_unaffected():
    """A door hold collecting a genuine app unlock (no HA unlock around) is
    unchanged."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t = CLOCK.now
    CLOCK.advance(2)
    door(hass, ctl, "on")
    CLOCK.advance(3)
    f61(ctl, dt=t); f62(ctl, dt=t)
    CLOCK.advance(controller_mod.DOOR_HOLD_S)
    rows = [r[0] for r in activity_rows(hass)]
    check("echo: door hold still resolves to the app unlock (one line)",
          rows == ["Unlocked (phone remote)"], rows)


ALL_TESTS = [t_messages, t_fingerprint_state_change, t_netted_fingerprint,
           t_buffered_flush_nets_to_locked, t_remote_dp61_dp62,
           t_remote_dp62_after_apply, t_lone_dp61, t_bare_dp11_wins_ordering,
           t_door_hold_fingerprint, t_door_hold_thumbturn,
           t_door_hold_intervening_lock, t_door_startup_ignored,
           t_door_close_never_unlocks_locked, t_ha_commands, t_timer_and_misc,
           t_dedupe,
           # fixes 1-5
           t_hold_then_live_lock_thumbturn, t_hold_then_live_lock_credential,
           t_hold_buffered_prior_lock_keeps_holding,
           t_hold_lock_within_slack_after_close,
           t_exit_resync_close_before_resolve, t_exit_resync_close_after_resolve,
           t_exit_resync_not_for_credential_close_before,
           t_fresh_lock_cancels_followup,
           t_wake_coalesce, t_wake_coalesce_boundary, t_polls_unchanged,
           t_listener_link_frames, t_offline_online, t_offline_implicit_online,
           t_unlock_while_unlocked_links, t_unlock_slow_refresh_links,
           t_unlock_failure_no_link, t_repeat_state_no_new_ctx,
           t_lock_entity_unavailable,
           # link: unknown after reconnect + connectivity sensor
           t_reconnect_unknown_then_ha_unlock, t_reconnect_unknown_then_device_dp,
           t_reconnect_unknown_then_door, t_reconnect_via_dp11_sets_state,
           t_reconnect_no_stale_relock, t_reconnect_no_stale_debounce,
           t_reconnect_hold_no_state, t_link_sensor,
           # MQTT: HA unlock over the listener's connection
           t_mqtt_unlock_on_listener, t_mqtt_frames_still_delivered,
           t_mqtt_unlock_no_echo, t_mqtt_unlock_waits_for_connection,
           t_mqtt_drop_unsent, t_mqtt_refresh_applied_to_listener,
           t_mqtt_standalone_cloud_lock, t_mqtt_tracker,
           # HA unlock: absorb the lock's response to our own command
           t_echo_dp61_before_return, t_echo_dp62_before_dp61,
           t_echo_both_after_return, t_echo_3s_after_confirm,
           t_echo_debounce_applies_in_flight, t_echo_genuine_app_unlock_later,
           t_echo_window_boundary, t_echo_fingerprint_not_swallowed,
           t_echo_buffered_replay_not_absorbed, t_echo_failed_then_late_echo,
           t_echo_failed_late_beyond_window, t_echo_unconfirmed_but_reported,
           t_echo_door_hold_unaffected]


def run(tests) -> int:
    for fn in tests:
        print(f"--- {fn.__name__}")
        fn()
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(ALL_TESTS))
