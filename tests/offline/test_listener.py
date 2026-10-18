"""H5: the MQTT listener survives bad frames, and its watchdog restarts a dead
paho thread without ever running two clients. Uses the shared harness's fake
paho client/broker (test_controller_harness.py).

Run (from the repo root):  python3 -I -B tests/offline/test_listener.py
"""
from __future__ import annotations

import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops it
import test_controller_harness as H  # noqa: E402
from test_controller_harness import FAKE_CLIENTS, check, listener_mod  # noqa: E402

logging.getLogger("era_lock.mqtt_listener").setLevel(logging.CRITICAL)  # quiet tracebacks


def frame(dps, t=1_800_000_000, dts=None):
    data = {"cid": "CID1", "dps": dps}
    if dts is not None:
        data["t"] = dts
    return {"protocol": 4, "t": t, "data": data}


def events(queued):
    return [a[2] for f, a in queued if f is listener_mod.async_dispatcher_send]


def deliver_raw(client, obj_or_bytes):
    """Deliver an arbitrary (possibly non-object) JSON payload."""
    import json
    pt = obj_or_bytes if isinstance(obj_or_bytes, bytes) else json.dumps(obj_or_bytes).encode()
    pad = 16 - len(pt) % 16
    client.on_message(client, None, H.FakeMsg(
        "smart/mb/in/CID1", b"2.2" + b"\0" * 12 + pt + bytes([pad]) * pad))


def t_malformed_frames():
    lst, client, mgr, queued = H.make_mqtt_listener()
    bad = [
        [1, 2, 3],                                     # non-object JSON
        "just a string",
        42,
        None,
        frame({"11": True}, dts={"11": "not-a-number"}),   # non-numeric data.t
        frame({"12": "AQ=="}, dts={"12": {"x": 1}}),
        {"protocol": 4, "t": "soon", "data": {"cid": "CID1", "dps": {"11": False}}},
        {"protocol": 4, "t": 5, "data": "nope"},
        {"protocol": 25, "t": 5, "data": {"online": "CID1"}},
    ]
    raised = []
    for b in bad:
        try:
            deliver_raw(client, b)
        except Exception as e:  # noqa: BLE001
            raised.append((b, e))
    deliver_raw(client, b"\xff\xfe not utf-8 json")
    check("listener: malformed frames never raise out of on_message (thread survives)",
          not raised, raised)
    evs = events(queued)
    check("listener: non-numeric data.t falls back to the frame time (still parsed)",
          any(e.type == "state_report" and e.locked is True and e.dt == 1_800_000_000
              for e in evs), [(e.type, e.dt) for e in evs])
    check("listener: non-numeric frame t falls back to HA's clock",
          any(e.type == "state_report" and e.locked is False
              and isinstance(e.dt, float) for e in evs), [(e.type, e.dt) for e in evs])
    queued.clear()
    client.deliver(frame({"11": False}, t=1_800_000_100, dts={"11": 1_800_000_100}))
    evs = events(queued)
    check("listener: a good frame after the bad ones is processed",
          [(e.type, e.locked, e.dt) for e in evs]
          == [("state_report", False, 1_800_000_100)], evs)


def t_handler_exception_contained():
    lst, client, mgr, queued = H.make_mqtt_listener()
    real = lst._handle
    calls = {"n": 0}

    def boom(obj):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyError("simulated parser bug")
        return real(obj)
    lst._handle = boom
    try:
        client.deliver(frame({"11": True}, dts={"11": 1_800_000_000}))
        err = None
    except Exception as e:  # noqa: BLE001
        err = e
    client.deliver(frame({"11": False}, t=1_800_000_005, dts={"11": 1_800_000_005}))
    evs = events(queued)
    check("listener: an exception in frame handling is logged, not propagated",
          err is None, err)
    check("listener: ...and the next frame is still handled",
          [(e.type, e.locked) for e in evs] == [("state_report", False)], evs)
    try:
        lst._on_connect(client, None, {}, types.SimpleNamespace(value="bad"), None)
        lst._on_disconnect(client, None, None, 0, None)
        err = None
    except Exception as e:  # noqa: BLE001
        err = e
    check("listener: on_connect / on_disconnect never raise either", err is None, err)


class Thread:
    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self):
        return self._alive


def t_watchdog_restart():
    lst, client, mgr, queued = H.make_mqtt_listener()
    jobs = []
    lst.hass.async_add_executor_job = lambda f, *a: jobs.append(f) or f(*a)
    torn = []
    client.loop_stop = lambda: torn.append("loop_stop")
    client.disconnect = lambda: torn.append("disconnect")
    client._sock_close = lambda: torn.append("sock_close")
    check("watchdog: (setup) suppress_exceptions set on the paho client",
          getattr(client, "suppress_exceptions", None) is True)
    client._thread = Thread(alive=True)
    lst._async_watchdog()
    check("watchdog: live thread -> nothing done",
          not jobs and len(FAKE_CLIENTS) == 1 and lst._client is client)
    client._thread = None   # paho's _thread_main clears it when loop_forever dies
    lst._async_watchdog()
    new = lst._client
    check("watchdog: dead thread -> listener restarted with a fresh client",
          len(jobs) == 1 and new is not client and len(FAKE_CLIENTS) == 2
          and new.client_id == client.client_id and new.on_message is not None,
          (jobs, FAKE_CLIENTS))
    check("watchdog: the dead client is torn down first (no second live client)",
          torn == ["loop_stop", "disconnect", "sock_close"]
          and client.on_message is None and client.on_connect is None, torn)
    check("watchdog: not connected until the new client's CONNACK",
          not lst.connected and not lst._restarting)
    lst._on_connect(new, None, {}, 0, None)
    new.responder = None
    queued.clear()
    new.deliver(frame({"11": True}, t=1_800_000_200, dts={"11": 1_800_000_200}))
    check("watchdog: frames flow on the restarted client",
          lst.connected and [(e.type, e.locked) for e in events(queued)]
          == [("state_report", True)], events(queued))
    new._thread = None
    lst.stop()
    n = len(FAKE_CLIENTS)
    lst._async_watchdog()
    lst.restart()
    check("watchdog: never restarts after stop() (unload)", len(FAKE_CLIENTS) == n)


def t_watchdog_restart_failure_retries():
    lst, client, mgr, queued = H.make_mqtt_listener()
    lst.hass.async_add_executor_job = lambda f, *a: f(*a)
    client._thread = None
    orig = mgr.mqtt_secrets
    mgr.mqtt_secrets = lambda: (_ for _ in ()).throw(RuntimeError("no session"))
    lst._async_watchdog()
    check("watchdog: a failed restart is logged and cleared for the next tick",
          not lst._restarting and lst._client is client)
    mgr.mqtt_secrets = orig
    lst._async_watchdog()
    check("watchdog: ...and the next tick restarts it",
          lst._client is not client and len(FAKE_CLIENTS) == 2)


ALL_TESTS = [t_malformed_frames, t_handler_exception_contained, t_watchdog_restart,
             t_watchdog_restart_failure_retries]

if __name__ == "__main__":
    sys.exit(H.run(ALL_TESTS))
