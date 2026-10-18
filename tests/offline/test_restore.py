"""v0.1.4: the last known lock state survives a restart (controller-owned
helpers.storage Store, seeded before the listener / startup sync), inferred and
lowest-priority: restore locked / unlocked, skip unknown / offline, buffered
replay overriding (and confirming) it under the restored device watermark, the
door-open-at-startup contradiction, and offline-at-startup.

Each "restart" is a fresh controller on the shared in-memory store
(test_controller_harness.STORE_DISK). Runs on the shared offline harness.

Run (from the repo root):  python3 -I -B tests/offline/test_restore.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I drops it
import test_controller_harness as H  # noqa: E402
from test_controller_harness import (  # noqa: E402
    CLOCK, STORE_DISK, activity_rows, check, controller_mod, door, flush_store,
    make, raw, state_changes, timeline,
)

logging.getLogger("era_lock").setLevel(logging.CRITICAL)  # (d): expected warning

KEY = "era_lock.E1"
RESTORED = "Restored after restart"
DOOR_OPEN = "Door open after restart"


def lines(hass):
    return [r[0] for r in activity_rows(hass)]


def restart(door_state=None):
    """HA stops (pending saves are written) and starts again: a new controller
    restores from the store BEFORE anything is reported. ``door_state`` is the
    door contact's state at startup ("on"/"off"/None = not loaded yet)."""
    flush_store()
    hass, ctl = make(keep_store=True)
    hass._door_state = (None if door_state is None
                        else types.SimpleNamespace(state=door_state))
    asyncio.run(ctl.async_restore())
    ent = H.make_lock_entity(hass, ctl)
    ent._apply_payload(ctl.snapshot())  # as async_added_to_hass does
    return hass, ctl, ent


def live_timers():
    return [t for t in CLOCK.timers if t[3]]


def first_life_locked():
    """A device-confirmed LOCKED (fingerprint unlock, then the lock's own
    lock); returns the device-time of the lock."""
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    t = CLOCK.now
    raw(ctl, type="unlock", dt=t, method="fingerprint", cred=1)
    CLOCK.advance(3)
    CLOCK.advance(10)
    raw(ctl, type="state_report", dt=CLOCK.now, locked=True)
    lock_dt = CLOCK.now
    CLOCK.advance(3)
    assert ctl._state == "locked" and not ctl._inferred, ctl.diagnostics()
    return lock_dt


def first_life_unlocked():
    hass, ctl = make()
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    unlock_dt = CLOCK.now
    CLOCK.advance(3)  # applied; relock failsafe still armed (HA stops now)
    assert ctl._state == "unlocked", ctl.diagnostics()
    return unlock_dt


# ------------------------------------------------------------------ tests --
def t_restore_locked():
    lock_dt = first_life_locked()
    flush_store()  # HA's final write
    stored = STORE_DISK.get(KEY)
    check("store: written on the final write: locked + watermark",
          stored and stored["state"] == "locked"
          and stored["last_device_dt"] == lock_dt, stored)
    hass, ctl, ent = restart(door_state="off")
    check("restore locked: state LOCKED, inferred, 'Restored after restart'",
          ctl._state == "locked" and ctl._inferred is True
          and ctl._changed_by == RESTORED and ctl._restored, ctl.diagnostics())
    check("restore locked: lock entity starts locked with reason + inferred attrs",
          ent.is_locked is True and ent.available
          and ent.extra_state_attributes["reason"] == RESTORED
          and ent.extra_state_attributes["inferred"] is True,
          ent.extra_state_attributes)
    check("restore locked: last unlock attributes restored",
          ent.extra_state_attributes["last_unlock_method"] == "fingerprint"
          and ent.extra_state_attributes["last_unlock_by"] == "Luke",
          ent.extra_state_attributes)
    check("restore locked: device watermark restored",
          ctl._last_device_dt == lock_dt, ctl._last_device_dt)
    check("restore: no activity line, no event-entity event, no wake/side effects",
          not activity_rows(hass)
          and not [r for r in hass.log if r[0] in ("event_entity", "wake")]
          and not live_timers(), hass.log)


def t_restore_unlocked():
    first_life_unlocked()
    hass, ctl, ent = restart(door_state="off")
    check("restore unlocked: state UNLOCKED, inferred, 'Restored after restart'",
          ctl._state == "unlocked" and ctl._inferred and ctl._changed_by == RESTORED
          and ent.is_locked is False, ctl.diagnostics())
    check("restore unlocked: the pre-restart failsafe is NOT re-armed",
          not live_timers() and ctl._relock_cancel is None, live_timers())
    CLOCK.advance(300)
    check("restore unlocked: no fabricated 'Auto-locked (assumed)' later",
          ctl._state == "unlocked" and not activity_rows(hass), lines(hass))


def t_restore_skipped_unknown():
    # (a) nothing stored (first install / pre-0.1.4): unknown, as before.
    hass, ctl = make()
    asyncio.run(ctl.async_restore())
    check("restore: empty store -> UNKNOWN", ctl._state is None
          and not ctl._restored)
    # (b) last state was unknown ("Lock state not confirmed", no door sensor).
    hass, ctl = make()
    ctl.entry.options = {k: v for k, v in ctl.entry.options.items()
                         if k != "door_contact_entity_id"}
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="unlock", dt=CLOCK.now, method="fingerprint", cred=1)
    CLOCK.advance(3 + 60)
    check("(setup) failsafe without door sensor -> unknown", ctl._state is None)
    hass, ctl, ent = restart()
    check("restore: last state UNKNOWN -> stays unknown (not the earlier unlocked)",
          ctl._state is None and not ctl._restored and ent.is_locked is None,
          (STORE_DISK.get(KEY), ctl.diagnostics()))
    # (c) the lock was offline (entity unavailable) when HA stopped.
    first_life_locked()
    hass, ctl = make(keep_store=True)
    ctl._state = ctl._logged_state = "locked"
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    hass, ctl, ent = restart()
    check("restore: lock offline at shutdown -> unknown on restart",
          ctl._state is None and not ctl._restored, STORE_DISK.get(KEY))
    # (d) an unreadable store doesn't block setup.
    STORE_DISK[KEY] = ValueError("corrupt")
    hass, ctl = make(keep_store=True)
    asyncio.run(ctl.async_restore())
    check("restore: unreadable store -> unknown, no exception", ctl._state is None)
    # (e) a watermark far in the future isn't trusted.
    STORE_DISK[KEY] = {"state": "locked", "last_device_dt": CLOCK.now + 7200,
                       "infer_t": 0}
    hass, ctl = make(keep_store=True)
    asyncio.run(ctl.async_restore())
    check("restore: watermark > 1h ahead of HA's clock is dropped",
          ctl._state == "locked" and ctl._last_device_dt == 0.0,
          ctl._last_device_dt)
    # (f) junk state values are ignored.
    STORE_DISK[KEY] = {"state": "unlocking"}
    hass, ctl = make(keep_store=True)
    asyncio.run(ctl.async_restore())
    check("restore: a non-real stored state -> unknown", ctl._state is None)


def t_replay_overrides_restored():
    lock_dt = first_life_locked()
    CLOCK.advance(600)  # HA down for 10 minutes; a card entry happened meanwhile
    hass, ctl, ent = restart(door_state="off")
    t = CLOCK.now
    # The startup sync flushes the buffered card unlock (no relock: the door
    # was opened, the lock waits for a handle-lift).
    raw(ctl, type="unlock", dt=t - 300, fresh=False, method="card", cred=51)
    raw(ctl, type="state_report", dt=t - 300, fresh=False, locked=False)
    CLOCK.advance(3)
    sc = state_changes(hass)
    rows = activity_rows(hass)
    check("replay: buffered card unlock -> its line, then UNLOCKED (device, not inferred)",
          [r[0] for r in rows] == ["Unlocked by Spare card (card)"]
          and ctl._state == "unlocked" and ctl._inferred is False
          and ctl._changed_by == "Synced from lock: unlocked" and not ctl._restored,
          (rows, ctl.diagnostics()))
    check("replay: the state row parents to the card line (cause before effect)",
          len(sc) == 1 and sc[0]["cause_ctx_id"] == rows[0][2]
          and timeline(hass) == [("line", "Unlocked by Spare card (card)"),
                                 ("state", "unlocked")], (sc, timeline(hass)))
    check("replay: buffered history fires no live side effects",
          ("wake", "on_unlock") not in hass.log and ctl._relock_cancel is None)


def t_replay_unlock_relock_nets_and_confirms():
    first_life_locked()
    CLOCK.advance(600)
    hass, ctl, ent = restart(door_state="off")
    t = CLOCK.now
    raw(ctl, type="unlock", dt=t - 300, fresh=False, method="fingerprint", cred=2)
    raw(ctl, type="state_report", dt=t - 300, fresh=False, locked=False)
    raw(ctl, type="state_report", dt=t - 270, fresh=False, locked=True)
    CLOCK.advance(3)
    check("replay unlock->relock: both lines, in device order",
          lines(hass) == ["Unlocked (fingerprint #2)", "Device locked"], lines(hass))
    check("replay unlock->relock: no state flicker (no new row), now device-synced",
          not state_changes(hass) and ctl._state == "locked"
          and ctl._inferred is False and ctl._changed_by == "Synced from lock: locked"
          and not ctl._restored, (state_changes(hass), ctl.diagnostics()))
    check("replay: the entity re-renders the confirmed reason on the same row",
          ent.rows and ent.rows[-1][0] == "locked"
          and ent.rows[-1][3]["inferred"] is False, ent.rows)


def t_replay_respects_restored_watermark():
    lock_dt = first_life_locked()
    hass, ctl, ent = restart(door_state="off")
    # A re-flush of history the controller already weighed before the restart:
    # the pre-restart unlock (older than the restored watermark) is not news.
    raw(ctl, type="state_report", dt=lock_dt - 13, fresh=False, locked=False)
    CLOCK.advance(3)
    check("watermark: a replayed report OLDER than the restored state is history",
          ctl._state == "locked" and ctl._restored and not state_changes(hass),
          ctl.diagnostics())
    # The same-second re-flush of the lock the restored state came from
    # confirms it.
    raw(ctl, type="state_report", dt=lock_dt, fresh=False, locked=True)
    CLOCK.advance(3)
    check("watermark: a re-flush of the restored lock confirms it (no new row)",
          ctl._state == "locked" and not ctl._inferred and not ctl._restored
          and ctl._changed_by == "Synced from lock: locked"
          and not state_changes(hass), ctl.diagnostics())


def t_live_events_override_restored():
    first_life_locked()
    CLOCK.advance(60)
    hass, ctl, ent = restart(door_state="off")
    raw(ctl, type="state_report", dt=CLOCK.now, locked=True)  # live lock-up
    CLOCK.advance(3)
    check("live: a fresh device lock confirms the restored LOCKED ('Device locked')",
          ctl._state == "locked" and not ctl._inferred
          and ctl._changed_by == "Device locked" and lines(hass) == ["Device locked"],
          (ctl.diagnostics(), lines(hass)))
    # restored UNLOCKED + Mark as locked: an HA inference replaces it.
    first_life_unlocked()
    hass, ctl, ent = restart(door_state="off")
    ctl.mark_locked("uid-luke")
    check("live: 'Mark as locked' replaces a restored UNLOCKED",
          ctl._state == "locked" and not ctl._restored
          and ctl._changed_by == "Marked as locked in Home Assistant")
    # restored LOCKED + a real door opening: the usual hold, then thumbturn.
    first_life_locked()
    CLOCK.advance(60)
    hass, ctl, ent = restart(door_state="off")
    door(hass, ctl, "on")
    CLOCK.advance(controller_mod.DOOR_HOLD_S + 1)
    check("live: door opened from a restored LOCKED -> hold -> thumbturn, unlocked",
          lines(hass) == ["Unlocked (thumbturn)"] and ctl._state == "unlocked"
          and timeline(hass)[-1] == ("state", "unlocked"), timeline(hass))


def t_door_open_at_startup():
    # Restored LOCKED, door contact reads OPEN at startup: contradiction.
    lock_dt = first_life_locked()
    CLOCK.advance(600)
    hass, ctl, ent = restart(door_state="on")
    check("door open at startup: restored LOCKED -> UNLOCKED, inferred, "
          "'Door open after restart'",
          ctl._state == "unlocked" and ctl._inferred
          and ctl._changed_by == DOOR_OPEN and ent.is_locked is False,
          ctl.diagnostics())
    check("door open at startup: no activity line / door event fabricated",
          not activity_rows(hass)
          and not [r for r in hass.log if r[0] == "event_entity"], hass.log)
    # A replayed pre-restart lock can't re-impose LOCKED on the open door.
    raw(ctl, type="state_report", dt=lock_dt + 1, fresh=False, locked=True)
    CLOCK.advance(3)
    check("door open at startup: a replayed older lock doesn't re-lock it",
          ctl._state == "unlocked", ctl.diagnostics())
    # Close + handle-lift: the lock's report wins as usual.
    door(hass, ctl, "off")
    CLOCK.advance(2)
    raw(ctl, type="state_report", dt=CLOCK.now, locked=True)
    CLOCK.advance(3)
    check("door open at startup: close + handle-lift -> 'Device locked'",
          ctl._state == "locked" and ctl._changed_by == "Device locked"
          and not ctl._inferred, ctl.diagnostics())
    # Restored UNLOCKED with the door open: no contradiction, kept as restored.
    first_life_unlocked()
    hass, ctl, ent = restart(door_state="on")
    check("door open at startup: restored UNLOCKED is kept",
          ctl._state == "unlocked" and ctl._changed_by == RESTORED)
    # Door sensor not loaded yet at restore; its first state write says open.
    first_life_locked()
    hass, ctl, ent = restart(door_state=None)
    check("(setup) door sensor not loaded: restored LOCKED as-is",
          ctl._state == "locked" and ctl._changed_by == RESTORED)
    door(hass, ctl, "on", old="none")
    sc = state_changes(hass)
    check("door sensor's first write = open: restored LOCKED -> UNLOCKED, one row",
          ctl._state == "unlocked" and ctl._changed_by == DOOR_OPEN
          and len(sc) == 1 and sc[0]["state"] == "unlocked"
          and not activity_rows(hass), (sc, ctl.diagnostics()))
    check("...and no hold / forced wake from a startup write",
          ctl._door_hold is None and not [r for r in hass.log if r[0] == "wake"])
    # First write = closed: nothing to resolve.
    first_life_locked()
    hass, ctl, ent = restart(door_state=None)
    door(hass, ctl, "off", old="none")
    check("door sensor's first write = closed: restored LOCKED kept",
          ctl._state == "locked" and ctl._restored and not state_changes(hass))


def t_offline_at_startup():
    first_life_locked()
    hass, ctl, ent = restart(door_state="off")
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=False)
    check("offline at startup: entity UNAVAILABLE, 'Lock offline (state unknown)'",
          ent.rows and ent.rows[-1][0] == "unavailable"
          and lines(hass) == ["Lock offline (state unknown)"], (ent.rows, lines(hass)))
    flush_store()
    check("offline at startup: the restored state is no longer persisted",
          STORE_DISK[KEY]["state"] is None, STORE_DISK[KEY])
    CLOCK.advance(30)
    raw(ctl, type="connectivity", dt=CLOCK.now, source="hub", value=True)
    check("offline at startup: reconnect -> UNKNOWN (not the restored LOCKED)",
          ctl._state is None and not ctl._restored
          and ctl._changed_by == "Lock back online (state unknown)"
          and ent.is_locked is None, ctl.diagnostics())


def t_persist_now():
    first_life_locked()
    flush_store()
    hass, ctl = make(keep_store=True)
    asyncio.run(ctl.async_restore())
    raw(ctl, type="unlock", dt=CLOCK.now, method="card", cred=51)
    CLOCK.advance(3)
    asyncio.run(ctl.async_persist())
    check("persist: async_persist writes the current state immediately",
          STORE_DISK[KEY]["state"] == "unlocked"
          and STORE_DISK[KEY]["last_method"] == "card", STORE_DISK[KEY])
    asyncio.run(ctl.async_remove_store())
    check("persist: entry removal drops the stored state", KEY not in STORE_DISK)


ALL_TESTS = [
    t_restore_locked, t_restore_unlocked, t_restore_skipped_unknown,
    t_replay_overrides_restored, t_replay_unlock_relock_nets_and_confirms,
    t_replay_respects_restored_watermark, t_live_events_override_restored,
    t_door_open_at_startup, t_offline_at_startup, t_persist_now,
]

if __name__ == "__main__":
    sys.exit(H.run(ALL_TESTS))
