"""v0.1.2 regression tests: the code review's probes P1-P7 plus the H1/H2/H3/H6
fixes, on the shared offline harness (test_controller_harness.py).

Run (from the repo root):  python3 -I -B tests/offline/test_regressions.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops it
import test_controller_harness as H  # noqa: E402
from test_controller_harness import (  # noqa: E402
    CLOCK, Context, HomeAssistantError, activity_rows, check, controller_mod, door,
    make, raw, state_changes,
)

DEBOUNCE = controller_mod.DEVICE_DEBOUNCE_S


def lines(hass):
    return [r[0] for r in activity_rows(hass)]


def wake_calls(hass):
    return [r[1] for r in hass.log if r[0] == "wake"]


def no_door_sensor(ctl):
    ctl.entry.options = {k: v for k, v in ctl.entry.options.items()
                         if k != "door_contact_entity_id"}


# ------------------------------------------------ H1: dropped handle-lift --
def t_p1_lift_right_after_close():
    """P1: handle-lift reported right after the door closes, its device-time
    floored to the second (so 0.6s 'before' HA's close timestamp)."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    hass._door_state = types.SimpleNamespace(state="on")
    CLOCK.now = 1_800_000_000.6
    door(hass, ctl, "off")
    raw(ctl, type="state_report", locked=True, dt=1_800_000_000.0, fresh=True)
    CLOCK.advance(5)
    check("P1: lift 0.6s 'before' the close (floored dt) -> LOCKED",
          ctl._state == "locked", ctl._state)
    check("P1: exactly one 'Device locked' line", lines(hass) == ["Device locked"],
          lines(hass))
    sc = state_changes(hass)
    check("P1: locked row device-confirmed, parented to the line",
          sc and sc[-1]["state"] == "locked" and sc[-1]["inferred"] is False
          and sc[-1]["cause_ctx_id"] == activity_rows(hass)[-1][2], sc)
    check("P1: polls/failsafe/follow-up cancelled once it settled",
          "cancel_polls" in wake_calls(hass) and ctl._relock_cancel is None,
          wake_calls(hass))


def t_p1b_skewed_device_clock():
    """P1b: device clock 3s behind HA, lift 1s after the close."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    hass._door_state = types.SimpleNamespace(state="on")
    door(hass, ctl, "off")
    raw(ctl, type="state_report", locked=True, dt=CLOCK.now + 1 - 3, fresh=True)
    CLOCK.advance(5)
    check("P1b: 3s device skew -> LOCKED, one 'Device locked' line",
          ctl._state == "locked" and lines(hass) == ["Device locked"],
          (ctl._state, lines(hass)))


def t_h1_close_from_unknown_then_lift():
    """Door close from UNKNOWN is a state-setting inference; a lift right after
    with a lagging device clock (within the slack) still wins."""
    hass, ctl = make()
    hass._door_state = types.SimpleNamespace(state="on")
    door(hass, ctl, "off")   # unknown -> "Door closed" (unlocked, inferred)
    raw(ctl, type="state_report", locked=True, dt=CLOCK.now - 3, fresh=True)
    CLOCK.advance(5)
    check("H1: close-from-unknown then lagging lift -> LOCKED",
          ctl._state == "locked" and lines(hass) == ["Door closed", "Device locked"],
          (ctl._state, lines(hass)))


def t_h1_stale_vs_inference_still_rejected():
    """A device lock from well BEFORE a door opening is history: it is logged
    but must not re-lock an open door, nor cancel anything."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    hass._door_state = types.SimpleNamespace(state="off")
    t0 = CLOCK.now
    CLOCK.advance(30)
    door(hass, ctl, "on")
    CLOCK.advance(2)
    raw(ctl, type="state_report", locked=True, dt=t0, fresh=True)   # 30s old
    CLOCK.advance(5)
    check("H1: pre-open lock (beyond slack) logged, state stays UNLOCKED",
          ctl._state == "unlocked" and lines(hass) == ["Device locked"]
          and "cancel_polls" not in wake_calls(hass), (ctl._state, lines(hass)))
    # within the slack, but the door is still open: the superseded relock
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    hass._door_state = types.SimpleNamespace(state="off")
    door(hass, ctl, "on")
    raw(ctl, type="state_report", locked=True, dt=CLOCK.now - 2, fresh=True)
    CLOCK.advance(5)
    check("H1: lock 2s 'before' an opening, door still open -> stays UNLOCKED",
          ctl._state == "unlocked", ctl._state)


def t_h1_ha_unlock_then_device_lock():
    """An HA unlock stamps HA's clock; the lock's own (live) auto-lock 30s
    later, whatever its clock skew within the slack, still lands."""
    hass, ctl = make()
    hass._door_state = types.SimpleNamespace(state="off")

    async def go():
        hass.async_add_executor_job = lambda f: asyncio.sleep(0)
        ctl.coordinator.async_request_refresh = lambda: asyncio.sleep(0)
        ctl._state = ctl._logged_state = "locked"
        await ctl.async_unlock("uid-julie", "Julie")
    asyncio.run(go())
    CLOCK.advance(30)
    raw(ctl, type="state_report", locked=True, dt=int(CLOCK.now) - 3)
    CLOCK.advance(5)
    check("H1: HA unlock -> device auto-lock 30s later -> LOCKED, confirmed",
          ctl._state == "locked" and lines(hass)
          == ["Unlocked in Home Assistant", "Device locked"]
          and hass.states_log[-1]["inferred"] is False, (ctl._state, lines(hass)))


# ------------------------------------------- H2: failsafe never invents --
def t_p4_failsafe_no_door_sensor():
    hass, ctl = make()
    no_door_sensor(ctl)
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    CLOCK.advance(65)
    check("P4: failsafe, no door sensor -> UNKNOWN (never LOCKED)",
          ctl._state is None, ctl._state)
    check("P4: one short 'Lock state not confirmed' line, no 'Auto-locked'",
          lines(hass) == ["Unlocked by Luke (fingerprint)", "Lock state not confirmed"],
          lines(hass))
    last = hass.states_log[-1]
    check("P4: unknown row is inferred, reason set, parented to that line",
          last["state"] is None and last["inferred"] is True
          and last["changed_by"] == "Lock state not confirmed"
          and last["cause_ctx_id"] == activity_rows(hass)[-1][2], last)
    ee = [r for r in hass.log if r[0] == "event_entity"]
    check("P4: event entity fires 'state_unconfirmed'",
          ee and ee[-1][1] == "state_unconfirmed", ee[-1:])
    CLOCK.advance(100)
    raw(ctl, type="state_report", locked=True, dt=CLOCK.now)  # e.g. a heartbeat sync
    CLOCK.advance(3)
    check("P4: a later device lock resolves UNKNOWN -> LOCKED",
          ctl._state == "locked" and lines(hass)[-1] == "Device locked", lines(hass))


def t_h2_door_sensor_unavailable():
    """Configured but not reporting open/closed: an opening may have been
    missed, so nothing can be assumed -> UNKNOWN."""
    hass, ctl = make()
    hass._door_state = types.SimpleNamespace(state="unavailable")
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    CLOCK.advance(65)
    check("H2: door sensor unavailable -> UNKNOWN", ctl._state is None
          and lines(hass)[-1] == "Lock state not confirmed", (ctl._state, lines(hass)))


def t_h2_door_stayed_shut_assumed_locked():
    hass, ctl = make()
    hass._door_state = types.SimpleNamespace(state="off")
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    CLOCK.advance(3)
    check("H2: (setup) unlocked, failsafe armed",
          ctl._state == "unlocked" and ctl._relock_cancel is not None)
    CLOCK.advance(62)
    last = hass.states_log[-1]
    check("H2: door sensor + door stayed shut -> LOCKED, inferred, 'Auto-locked (assumed)'",
          ctl._state == "locked" and last["inferred"] is True
          and last["changed_by"] == "Auto-locked (assumed)"
          and lines(hass) == ["Unlocked by Luke (fingerprint)", "Auto-locked (assumed)"]
          and last["cause_ctx_id"] == activity_rows(hass)[-1][2], (lines(hass), last))
    raw(ctl, type="state_report", locked=True, dt=CLOCK.now)  # the lock confirms
    CLOCK.advance(3)
    last = hass.states_log[-1]
    check("H2: a later device lock confirms the assumption (inferred -> False)",
          ctl._state == "locked" and last["inferred"] is False
          and last["changed_by"] == "Device locked", last)


def t_p7_entry_no_lift():
    """P7: fingerprint entry; door open + close quickly; the flush lands AFTER
    the close; nobody lifts the handle. The lock can't auto-lock after an
    opening, so it must stay UNLOCKED (not assumed locked, not unknown)."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    hass._door_state = types.SimpleNamespace(state="off")
    t0 = CLOCK.now
    door(hass, ctl, "on"); CLOCK.advance(2)
    door(hass, ctl, "off"); CLOCK.advance(1.5)
    raw(ctl, type="unlock", dt=int(t0) - 2, method="fingerprint", cred=1, fresh=True)
    raw(ctl, type="state_report", locked=False, dt=int(t0) - 2, fresh=True)
    CLOCK.advance(90)
    check("P7: entry, no lift, +90s -> stays UNLOCKED",
          ctl._state == "unlocked", ctl._state)
    check("P7: no 'Auto-locked' / 'not confirmed' line",
          lines(hass) == ["Unlocked by Luke (fingerprint)"], lines(hass))
    CLOCK.advance(300)
    raw(ctl, type="state_report", locked=True, dt=CLOCK.now)  # handle lifted later
    CLOCK.advance(3)
    check("P7: a later dp11:true still resolves it to LOCKED",
          ctl._state == "locked" and lines(hass)[-1] == "Device locked"
          and hass.states_log[-1]["inferred"] is False, lines(hass))


def t_h2_door_opened_after_unlock():
    """The ordinary entry order (unlock reported, then the door opens): the
    opening cancels the failsafe; stays UNLOCKED until the lock reports."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    hass._door_state = types.SimpleNamespace(state="off")
    raw(ctl, type="unlock", dt=CLOCK.now, method="card", cred=51)
    CLOCK.advance(4)
    door(hass, ctl, "on"); CLOCK.advance(3)
    door(hass, ctl, "off")
    CLOCK.advance(200)
    check("H2: unlock -> open -> close -> +200s: UNLOCKED, no assumed lock",
          ctl._state == "unlocked" and lines(hass) == ["Unlocked by Spare card (card)"]
          and ctl._relock_cancel is None, (ctl._state, lines(hass)))


# ------------------------------------------------------ H3: dp11 bounce --
def _p5_frames(ctl, T, *, split):
    raw(ctl, type="unlock", dt=T, method="remote")
    raw(ctl, type="unlock", dt=T, method="phone_remote", cred=0)
    raw(ctl, type="state_report", locked=False, dt=T)
    raw(ctl, type="state_report", locked=True, dt=T + split)
    raw(ctl, type="state_report", locked=False, dt=T + split)


def t_p5_bounce():
    """P5: remote unlock where dp11 bounces false -> true -> false (observed
    2026-10-16), the bounce in the next device-second."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    _p5_frames(ctl, int(CLOCK.now), split=1)
    CLOCK.advance(10)
    check("P5: ends UNLOCKED", ctl._state == "unlocked", ctl._state)
    check("P5: one line 'Unlocked (phone remote)', NO 'Device locked'",
          lines(hass) == ["Unlocked (phone remote)"], lines(hass))
    check("P5: polls + failsafe still armed (bounce cancelled nothing)",
          "cancel_polls" not in wake_calls(hass) and "on_unlock" in wake_calls(hass)
          and ctl._relock_cancel is not None, (wake_calls(hass), ctl._relock_cancel))
    sc = state_changes(hass)
    check("P5: one state row (unlocked), parented to the phone-remote line",
          [s["state"] for s in sc] == ["unlocked"]
          and sc[0]["cause_ctx_id"] == activity_rows(hass)[0][2], sc)
    hass._door_state = types.SimpleNamespace(state="off")
    CLOCK.advance(120)
    check("P5: +120s, door shut -> the failsafe resolves it (assumed locked)",
          ctl._state == "locked" and lines(hass)[-1] == "Auto-locked (assumed)",
          lines(hass))


def t_p5b_bounce_same_second():
    """P5b: the whole bounce stamped in ONE device-second: ties go to arrival
    order, so the last report (false) wins."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    _p5_frames(ctl, int(CLOCK.now), split=0)
    CLOCK.advance(10)
    check("P5b: same-second bounce -> UNLOCKED, no lock line, polls armed",
          ctl._state == "unlocked" and lines(hass) == ["Unlocked (phone remote)"]
          and "cancel_polls" not in wake_calls(hass)
          and ctl._relock_cancel is not None, (ctl._state, lines(hass)))
    CLOCK.advance(20)
    raw(ctl, type="state_report", locked=True, dt=int(CLOCK.now))  # +30s poll
    CLOCK.advance(3)
    check("P5b: the real auto-lock caught by a later poll -> LOCKED, one lock line",
          ctl._state == "locked"
          and lines(hass) == ["Unlocked (phone remote)", "Device locked"]
          and "cancel_polls" in wake_calls(hass), lines(hass))


def t_h3_tie_after_settle_is_reflush():
    """A same-second report arriving after the burst settled is a re-flush,
    not a new state."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    T = int(CLOCK.now)
    raw(ctl, type="state_report", locked=False, dt=T)
    raw(ctl, type="state_report", locked=True, dt=T)
    CLOCK.advance(5)
    check("H3: (setup) same-second false,true -> LOCKED", ctl._state == "locked")
    raw(ctl, type="state_report", locked=False, dt=T)   # re-flush, burst over
    CLOCK.advance(5)
    check("H3: a same-second re-flush after settling doesn't flip the state",
          ctl._state == "locked", ctl._state)


def t_h3_stale_flush_unchanged():
    """Buffered (stale) flushes still emit every discrete event and settle."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "unlocked"
    t = CLOCK.now - 200
    raw(ctl, type="state_report", locked=True, dt=t, fresh=False)
    raw(ctl, type="unlock", dt=t + 50, fresh=False, method="card", cred=51)
    raw(ctl, type="state_report", locked=False, dt=t + 50, fresh=False)
    raw(ctl, type="state_report", locked=True, dt=t + 80, fresh=False)
    CLOCK.advance(5)
    check("H3: stale flush lines in arrival order, settles LOCKED (synced)",
          lines(hass) == ["Device locked", "Unlocked by Spare card (card)",
                          "Device locked"]
          and ctl._state == "locked"
          and hass.states_log[-1]["changed_by"] == "Synced from lock: locked",
          (lines(hass), hass.states_log[-1:]))


def t_p3_stale_lock_cancels_polls():
    """P3: a buffered lock newer than the unlock settles LOCKED: the
    post-unlock polls are then moot and are cancelled (battery)."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1, fresh=True)
    CLOCK.advance(3)
    raw(ctl, type="state_report", locked=True, dt=CLOCK.now, fresh=False)
    CLOCK.advance(3)
    check("P3: unlock then newer stale lock -> LOCKED, polls + failsafe cancelled",
          ctl._state == "locked"
          and lines(hass) == ["Unlocked by Luke (fingerprint)", "Device locked"]
          and "cancel_polls" in wake_calls(hass) and ctl._relock_cancel is None,
          (lines(hass), wake_calls(hass)))


# ------------------------------------------------------- P2 / P6 notes --
def t_p2_echo_in_flight():
    """P2/P2b (realistic form): the lock's echo can only follow our dp61
    publish, i.e. arrive while the command is in flight (the controller marks
    the unlock in flight before publishing). The reviewer's literal P2 fed the
    echo BEFORE async_unlock was called, which can't happen."""
    for with_dp62 in (False, True):
        hass, ctl = make()
        ctl._state = ctl._logged_state = "locked"

        def during():
            raw(ctl, type="unlock", dt=int(CLOCK.now), method="remote")
            if with_dp62:
                raw(ctl, type="unlock", dt=int(CLOCK.now), method="phone_remote",
                    cred=0)
        H.ha_unlock(hass, ctl, during=during)
        CLOCK.advance(5)
        check(f"P2{'b' if with_dp62 else ''}: echo in flight -> one HA line",
              [(r[0], r[1]) for r in activity_rows(hass)]
              == [("Unlocked in Home Assistant", "uid-julie")], activity_rows(hass))


def t_p6_restart_unknown():
    hass, ctl = make()
    check("P6: after a restart the state is UNKNOWN until the first event",
          ctl._state is None and not activity_rows(hass))


# --------------------------------------------- H6: lock / mark as locked --
def t_h6_lock_service_raises():
    hass, ctl = make()
    ent = H.make_lock_entity(hass, ctl)
    ctl._state = ctl._logged_state = ent._state = "unlocked"
    n_rows, n_log = len(ent.rows), len(hass.log)

    async def go():
        ent.async_set_context(Context(user_id="uid-julie"))
        await ent.async_lock()
    try:
        asyncio.run(go())
        err = None
    except HomeAssistantError as e:
        err = e
    check("H6/v0.1.3: lock.lock raises ServiceValidationError (not logged at "
          "ERROR) with a translation key",
          isinstance(err, H.ServiceValidationError)
          and err.translation_domain == "era_lock"
          and err.translation_key == "lock_not_supported", err)
    check("H6: ...and changes nothing (state, rows, activity)",
          ctl._state == "unlocked" and len(ent.rows) == n_rows
          and len(hass.log) == n_log and not activity_rows(hass), hass.log[n_log:])


def t_h6_mark_locked_button():
    hass, ctl = make()
    coord = ctl.coordinator
    coord.controller = ctl
    entry = types.SimpleNamespace(entry_id="E1", unique_id="HUB")
    btn = H.button_mod.EraMarkLockedButton(coord, entry)
    btn.hass = hass
    ctl._state = ctl._logged_state = "unlocked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="card", cred=51)
    CLOCK.advance(3)
    check("H6: button unique id / translation key / entity name",
          btn._attr_unique_id == "HUB_mark_locked"
          and btn._attr_translation_key == "mark_locked" and btn._attr_has_entity_name
          and btn._attr_device_info
          == H.importlib.import_module("era_lock.entity").device_info(entry),
          btn._attr_unique_id)

    async def press():
        btn.async_set_context(Context(user_id="uid-julie"))  # HA does this
        await btn.async_press()
    asyncio.run(press())
    rows = activity_rows(hass)
    last = hass.states_log[-1]
    check("H6: press -> 'Marked as locked in Home Assistant' by the presser",
          rows[-1][0] == "Marked as locked in Home Assistant"
          and rows[-1][1] == "uid-julie", rows)
    check("H6: ...state LOCKED, inferred, parented to that line, By the presser",
          ctl._state == "locked" and last["state"] == "locked"
          and last["inferred"] is True and last["cause_ctx_id"] == rows[-1][2]
          and last["user_id"] == "uid-julie"
          and last["changed_by"] == "Marked as locked in Home Assistant", last)
    check("H6: ...and the failsafe is cancelled", ctl._relock_cancel is None)
    raw(ctl, type="state_report", locked=False, dt=CLOCK.now - 30, fresh=False)
    CLOCK.advance(3)
    check("H6: an older buffered report doesn't undo the assertion",
          ctl._state == "locked", ctl._state)
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    CLOCK.advance(3)
    check("H6: a newer real unlock still wins", ctl._state == "unlocked")


ALL_TESTS = [
    t_p1_lift_right_after_close, t_p1b_skewed_device_clock,
    t_h1_close_from_unknown_then_lift, t_h1_stale_vs_inference_still_rejected,
    t_h1_ha_unlock_then_device_lock,
    t_p4_failsafe_no_door_sensor, t_h2_door_sensor_unavailable,
    t_h2_door_stayed_shut_assumed_locked, t_p7_entry_no_lift,
    t_h2_door_opened_after_unlock,
    t_p5_bounce, t_p5b_bounce_same_second, t_h3_tie_after_settle_is_reflush,
    t_h3_stale_flush_unchanged, t_p3_stale_lock_cancels_polls,
    t_p2_echo_in_flight, t_p6_restart_unknown,
    t_h6_lock_service_raises, t_h6_mark_locked_button,
]

if __name__ == "__main__":
    sys.exit(H.run(ALL_TESTS))
