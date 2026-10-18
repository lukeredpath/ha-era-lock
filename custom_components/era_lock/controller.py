"""Decision layer (Layer 2) for the ERA Protect Lock.

The controller owns ALL logic. It subscribes to ``signal_raw`` (normalised
:class:`LockEvent`s from the sources) and to the optional door-contact entity,
runs the inferred-state machine (device-time ordering + debounce, fresh-vs-stale
handling, auto-relock failsafe, credential auto-discovery, attribution), drives
the wake manager, and performs the cloud unlock command. It emits two outputs:

  - ``signal_event``  enriched discrete activity events for the event entity;
  - ``signal_state``  state-model updates the lock entity renders verbatim;
  - ``signal_link``   the lock's link state (hub presence) for the connectivity
    binary sensor.

Attribution linkage: every discrete activity event carries a cause ``Context``
id (``cause_ctx``). The event entity publishes the activity with that id — both
its own state change and the rich ``EVENT_ACTIVITY`` logbook row ("Unlocked by
Luke (fingerprint)") — and a lock state change caused by that activity is
published with the same id as its parent, so it is shown "triggered by" / "By
<user>" its activity. INVARIANT: a state transition only ever parents to the
context of a discrete event that is (or will be) actually emitted for it — see
the dp61/method-DP coalescer and the door-open hold, which both reuse one
reserved context for whichever event finally represents the physical unlock.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)

from . import activity
from .attribution import actor as attr_actor
from .attribution import as_int, lookup
from .const import (
    CONF_AUTO_RELOCK_SECONDS,
    CONF_CREDENTIALS,
    CONF_DOOR_CONTACT_ENTITY_ID,
    CREDENTIAL_METHODS,
    DEFAULT_AUTO_RELOCK_SECONDS,
    DOMAIN,
)
from .eralock.mqtt import CloudMqttLock, MqttUnlockError
from .events import LockEvent, signal_event, signal_link, signal_raw, signal_state
from .wake import POST_CLOSE_SYNC_S

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import EraLockCoordinator

_LOGGER = logging.getLogger(__name__)

LOCKED = "locked"
UNLOCKED = "unlocked"
DEVICE_DEBOUNCE_S = 2.0  # coalesce dp11 mini-history bursts into one transition
# A single remote/app unlock is reported as TWO separate MQTT frames ~1s apart
# with the SAME device-time: the dp61 "AEkA" transport echo (method="remote", no
# credential) and a method DP (dp62 phone_remote / dp12 fingerprint / ...) that
# carries the real attribution. Coalesce discrete unlock events whose device-times
# fall within this window into ONE physical unlock; a real method DP beats the bare
# "remote" echo (see _emit_unlock_discrete). Unlocks further apart stay distinct.
UNLOCK_COALESCE_S = 2.0

# Door opened while we believe the lock is LOCKED: the door can't be open with the
# deadbolt thrown, so the state is UNLOCKED at once — but HOW it was unlocked is
# ambiguous. The lock buffers local actions (fingerprint/card) while asleep, so a
# credential unlock may still be in flight; only if none turns up is it the
# internal thumbturn (the one manual path that reports no DP). So the unlock's
# attribution + discrete event are HELD for DOOR_HOLD_S while the door-triggered
# wake flushes the buffer. Flushes typically land ~3s after a wake (plus up to
# UNLOCK_COALESCE_S for a dp61/dp62 pair); 10s gives ~3x headroom for a slow BLE
# reconnect, and the cost of waiting is only a later activity line — the lock
# state itself is already correct.
DOOR_HOLD_S = 10.0
# A credential unlock explains the door opening if its device-time is up to this
# long BEFORE the open (people unlock, then open). Matches the 60s minimum relock
# failsafe: beyond that the lock would have auto-relocked (and an intervening
# device lock disqualifies the credential anyway — see _pick_door_cause).
DOOR_LOOKBACK_S = 60.0
# Device clock vs HA wall-clock skew allowance (a credential "after" the open by
# less than this still counts as before it).
DOOR_CLOCK_SLACK_S = 5.0
# Unlock methods that can explain a door opening from locked. "remote" is the
# bare dp61 echo — a lone echo is still a real remote unlock.
DOOR_CAUSE_METHODS = ("fingerprint", "card", "ble", "phone_remote", "voice", "remote")
# A device lock reported while a door hold is pending is one of two things:
#   - a buffered relock from BEFORE the open (device-time < open): it disqualifies
#     the credentials before it (see _pick_door_cause) and the hold carries on;
#   - a lock-up AFTER the open (closed the door, lifted the handle): the unlock
#     that the hold represents happened first, so the hold is resolved BEFORE the
#     lock's activity line is emitted (the log must never read lock -> unlock).
# See _lock_after_open for the boundary.

# An HA unlock rides the listener's own MQTT connection, so the listener HEARS
# the lock's response to our command: the dp61 "AEkA" echo (often before the
# command even returns — it is what confirms it) and ~100ms-1s later the dp62
# phone_remote method DP (cred 0), plus a bare dp11:false. Those are our echo,
# not a second (app) unlock. A remote-type unlock is absorbed into the HA unlock
# while the command is in flight and for HA_ECHO_GRACE_S after it returns.
# 5s: >2x the UNLOCK_COALESCE_S dp61/dp62 pairing window and comfortably past
# the observed ~100ms dp62 lag plus broker/loop latency, while an INDEPENDENT
# app unlock of an already-unlocking door within 5s of an HA unlock is
# implausible (and, if it happened, costs only one redundant line). Only
# remote-type methods are echoes: a fingerprint/card/BLE unlock in the window is
# a real, separate action. "voice" is excluded too — HA's command never
# produces it, so a voice unlock is always someone else.
HA_ECHO_GRACE_S = 5.0
HA_ECHO_METHODS = ("remote", "phone_remote")


class _HaUnlock:
    """An HA unlock command: in flight, then its echo grace window."""

    __slots__ = ("ctx_id", "user_id", "user_name", "start_t", "end_t",
                 "emitted", "echoed", "ev")

    def __init__(self, ctx_id: str, user_id: str | None, user_name: str,
                 start_t: float) -> None:
        self.ctx_id = ctx_id        # reserved ctx: the ONE activity line + state
        self.user_id = user_id
        self.user_name = user_name
        self.start_t = start_t      # wall-clock before the command was published
        self.end_t: float | None = None  # wall-clock the command returned/raised
        self.emitted = False        # its "Unlocked in Home Assistant" line is out
        self.echoed = False         # the lock reported an unlock for it
        self.ev: LockEvent | None = None  # the emitted command LockEvent


class _DoorHold:
    """A door-open-from-locked whose unlock attribution is being held."""

    __slots__ = ("ctx_id", "open_t", "closed_t", "candidates", "lock_dts", "cancel")

    def __init__(self, ctx_id: str, open_t: float) -> None:
        self.ctx_id = ctx_id          # reserved cause ctx (state already parents to it)
        self.open_t = open_t          # wall-clock time the door opened
        self.closed_t: float | None = None  # wall-clock close while held (None = open)
        self.candidates: list[tuple[LockEvent, str | None]] = []
        self.lock_dts: list[float] = []  # device locks seen in the lookback
        self.cancel = None

    def in_window(self, dt: float) -> bool:
        return (self.open_t - DOOR_LOOKBACK_S) <= dt <= (self.open_t + DOOR_CLOCK_SLACK_S)

    def accepts(self, ev: LockEvent) -> bool:
        return ev.method in DOOR_CAUSE_METHODS and self.in_window(ev.dt)


def _pick_door_cause(
    candidates: list[tuple[LockEvent, str | None]], lock_dts: list[float],
    open_t: float,
) -> tuple[LockEvent, str | None] | None:
    """Choose the credential unlock that explains a door opening, if any.

    A candidate is disqualified by a device lock between it and the open (the
    lock relocked, so something else unlocked it again). Of the rest the latest
    wins; within UNLOCK_COALESCE_S of it a real method DP beats the bare dp61
    "remote" echo (same rule as _emit_unlock_discrete)."""
    valid = [c for c in candidates
             if not any(c[0].dt < ldt < open_t for ldt in lock_dts)]
    if not valid:
        return None
    latest = max(c[0].dt for c in valid)
    group = [c for c in valid if latest - c[0].dt <= UNLOCK_COALESCE_S]
    pool = [c for c in group if c[0].method != "remote"] or group
    return max(pool, key=lambda c: c[0].dt)


class EraController:
    """The lock's decision layer: state machine, commands, wake + attribution."""

    def __init__(
        self, hass: HomeAssistant, entry: "ConfigEntry",
        coordinator: "EraLockCoordinator",
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.entry_id = entry.entry_id
        self.coordinator = coordinator
        # -- state model ------------------------------------------------------
        self._state: str | None = None
        self._changed_by: str | None = None
        self._inferred: bool = True  # True until the lock itself confirms a state
        self._last_method: str | None = None
        self._last_user: str | None = None
        self._last_applied_dt: float = 0.0  # device-time of the last applied state
        self._pending: LockEvent | None = None
        self._pending_ctx_id: str | None = None  # ctx of the pending event's activity row
        self._device_debounce = None
        # -- unlock-event coalescing (dp61 echo + method DP -> one event) -----
        self._unlock_echo = None            # buffered (ev, ctx_id, user_id) echo
        self._unlock_echo_cancel = None     # its flush timer
        self._last_unlock_discrete_dt: float | None = None  # dt of last unlock emitted
        # (ev, ctx_id, user_id) of the last unlock discrete event emitted
        self._last_unlock: tuple[LockEvent, str | None, str | None] | None = None
        # ctx of the current UNLOCKED state whose reason may still be refined by
        # a better-attributed event for the same physical unlock (a method DP
        # superseding the dp61 echo after the state applied; a resolved door
        # hold). None otherwise.
        self._attributable_ctx_id: str | None = None
        self._door_hold: _DoorHold | None = None  # door-open-from-locked hold
        self._ha_unlock: _HaUnlock | None = None  # latest HA unlock (see HA_ECHO_GRACE_S)
        # A door-open-from-locked RESOLVED as a thumbturn exit while the door is
        # still open: the close arms the post-close re-sync.
        self._exit_pending: bool = False
        # The hub's view of the lock's link. While offline, actions at the door
        # are neither reported nor buffered, so the state can't be trusted: the
        # lock entity goes unavailable (see _set_online). Assumed online at
        # startup: the hub only reports presence on change.
        self._online: bool = True
        self._relock_cancel = None
        self._logged_state: str | None = "__init__"  # state-change de-dupe
        # -- subscriptions ----------------------------------------------------
        self._unsub_raw = None
        self._unsub_door = None

    @property
    def online(self) -> bool:
        """The hub's view of the lock's link (True = awake or asleep/nearby)."""
        return self._online

    # -- options --------------------------------------------------------------
    @property
    def _options(self) -> dict:
        return dict(self.entry.options)

    @property
    def _relock_seconds(self) -> int:
        try:
            v = int(self._options.get(CONF_AUTO_RELOCK_SECONDS, DEFAULT_AUTO_RELOCK_SECONDS))
        except (TypeError, ValueError):
            v = DEFAULT_AUTO_RELOCK_SECONDS
        return max(v, 60)  # failsafe only; the device-poll should capture the real lock first

    @property
    def _door_entity(self) -> str | None:
        return self._options.get(CONF_DOOR_CONTACT_ENTITY_ID)

    # -- lifecycle ------------------------------------------------------------
    @callback
    def start(self) -> None:
        self._unsub_raw = async_dispatcher_connect(
            self.hass, signal_raw(self.entry_id), self._on_raw)
        if self._door_entity:
            self._unsub_door = async_track_state_change_event(
                self.hass, [self._door_entity], self._on_door)

    @callback
    def stop(self) -> None:
        if self._unsub_raw is not None:
            self._unsub_raw()
            self._unsub_raw = None
        if self._unsub_door is not None:
            self._unsub_door()
            self._unsub_door = None
        self._cancel_relock()
        self._cancel_device_debounce()
        self._cancel_unlock_echo()
        if self._door_hold is not None and self._door_hold.cancel is not None:
            self._door_hold.cancel()
        self._door_hold = None

    # -- transition + outputs -------------------------------------------------
    @callback
    def _transition(
        self, state: str | None, reason: str, *, inferred: bool = False,
        user_id: str | None = None, cause_event: LockEvent | None = None,
        discrete: str | None = None, cause_ctx_id: str | None = None,
    ) -> str | None:
        """Change state and attribute it to the event that caused it.

        Pass ``cause_ctx_id`` when the discrete activity event was (or will be)
        published elsewhere — listener-sourced events on arrival, a held door
        unlock on resolution — or ``discrete`` + ``cause_event`` for a direct
        transition (command, timer) whose discrete event is published here.
        The discrete event is ALWAYS published when given (so an HA unlock while
        already unlocked still gets its activity line), and the state publish
        then ALWAYS parents to its context — even when the controller's state
        doesn't change, the entity's visible row may (an HA unlock's spinner
        unlocking -> unlocked), and it must link to the activity line. Without
        an emitted activity, the publish parents to ``cause_ctx_id`` (or a fresh
        context) only when the state actually changes (de-dupe by
        ``_logged_state``); a repeat keeps the entity's existing context.
        ``inferred`` marks a state HA deduced (door sensor, relock timer, manual
        assertion). Returns the context id the publish parented to, if any."""
        self._changed_by = reason
        self._inferred = inferred
        _LOGGER.debug("lock -> %s  (%s)%s", state or "unknown", reason,
                      " [inferred]" if inferred else "")
        emitted = False
        if discrete and cause_event is not None:
            if cause_ctx_id is None:
                cause_ctx_id = Context(user_id=user_id).id
            self._emit_event(discrete, cause_event,
                             cause_ctx_id=cause_ctx_id, user_id=user_id)
            emitted = True
        if state != self._logged_state:
            self._logged_state = state
            self._attributable_ctx_id = None
            if cause_ctx_id is None:
                # No activity behind this change (e.g. a bare dp11 sync): still
                # give the state row a fresh context so it can't inherit one.
                cause_ctx_id = Context(user_id=user_id).id
        elif not emitted:
            cause_ctx_id = None  # repeat of the same logged state: no new cause
        self._state = state
        self._publish_state(cause_ctx_id, user_id)
        return cause_ctx_id

    @callback
    def _publish_state(self, cause_ctx_id: str | None, user_id: str | None) -> None:
        async_dispatcher_send(self.hass, signal_state(self.entry_id), {
            "state": self._state,
            "changed_by": self._changed_by,
            "inferred": self._inferred,
            "last_method": self._last_method,
            "last_user": self._last_user,
            "cause_ctx_id": cause_ctx_id,
            "user_id": user_id,
            "available": self._online,
        })

    @callback
    def _refine_unlock(self, ctx_id: str | None, ev: LockEvent) -> None:
        """A better-attributed event for the unlock behind the current UNLOCKED
        state was emitted under the same ``ctx_id`` (a method DP superseding the
        dp61 echo after the state applied; a resolved door hold): update the
        reason / last-unlock attributes. No new context — the state row already
        parents to ``ctx_id`` and the state itself doesn't change."""
        if (ctx_id is None or self._state != UNLOCKED
                or self._attributable_ctx_id != ctx_id):
            return
        reason, display, _uid = self._attribute_unlock(ev)
        self._changed_by = reason
        self._last_method = ev.method
        self._last_user = display
        self._publish_state(None, None)

    @callback
    def _emit_event(
        self, etype: str, ev: LockEvent, *, cause_ctx_id: str | None,
        user_id: str | None,
    ) -> None:
        """Publish an enriched discrete activity event. Attribution (label/user/
        actor) and the rich activity line are resolved here so the event entity
        needs no options access."""
        creds = self._options.get(CONF_CREDENTIALS, [])
        label, mapped_uid, user_name = lookup(creds, ev.method, ev.cred)
        # Only name an actor when there's a real one to name; a plain
        # lock/failed_unlock/tamper/door carries no credential, so "unknown"
        # is dropped to None rather than surfaced as a spurious attribute.
        actor = attr_actor(creds, ev.method, ev.cred, ev.who)
        if actor == "unknown":
            actor = None
        message = activity.describe(
            etype, method=ev.method, cred=ev.cred, source=ev.source,
            subtype=ev.subtype, label=label, user_name=user_name,
            user_mapped=bool(mapped_uid), who=ev.who)
        if etype == "unlock":
            self._last_unlock = (ev, cause_ctx_id, user_id or mapped_uid)
            self._last_unlock_discrete_dt = ev.dt
        async_dispatcher_send(self.hass, signal_event(self.entry_id), {
            "type": etype,
            "dt": ev.dt,
            "method": ev.method,
            "cred": ev.cred,
            "who": ev.who,
            "user": user_name,
            "user_id": user_id or mapped_uid,
            "label": label,
            "actor": actor,
            "source": ev.source,
            "subtype": ev.subtype,
            "message": message,
            "cause_ctx_id": cause_ctx_id,
        })

    # -- unlock-event coalescing ----------------------------------------------
    @callback
    def _emit_unlock_discrete(
        self, ev: LockEvent, ctx_id: str, user_id: str | None,
    ) -> str | None:
        """Emit exactly one discrete ``unlock`` event per physical unlock, and
        return the context id that physical unlock's event uses (the caller
        parents the state transition to it).

        A single remote/app unlock reaches us as TWO ``unlock`` LockEvents from
        two separate MQTT frames ~1s apart with the SAME device-time: the dp61
        ``AEkA`` transport echo (``method="remote"``, no credential) and a method
        DP (dp62 phone_remote / dp12 fingerprint / ...) carrying the real
        attribution. The listener only sees one frame at a time, so the coalesce
        has to happen here, over the event stream.

        Precedence: a real METHOD DP beats the bare ``remote`` echo.
          * method DP -> emit immediately; a buffered echo for the same unlock is
            dropped and the method DP is emitted UNDER THE ECHO'S CONTEXT (the
            echo arrived first, so it usually won the device-time ordering and
            the pending state transition already parents to its context). A
            later echo for an already-emitted unlock (within UNLOCK_COALESCE_S)
            is suppressed and resolves to the emitted event's context.
          * echo -> hold for UNLOCK_COALESCE_S. A method DP arriving in that
            window supersedes it (as above); otherwise the echo is emitted, so a
            lone/app-free dp61 remote unlock (no method DP) still surfaces.
        Unlocks whose device-times are more than UNLOCK_COALESCE_S apart are
        distinct physical unlocks and each emit.
        """
        if ev.method == "remote":  # the bare dp61 echo
            if (self._last_unlock_discrete_dt is not None
                    and abs(ev.dt - self._last_unlock_discrete_dt)
                    <= UNLOCK_COALESCE_S):
                # a method DP / command for this unlock already emitted
                return self._last_unlock[1] if self._last_unlock else None
            held = self._unlock_echo
            if held is not None:
                if abs(ev.dt - held[0].dt) <= UNLOCK_COALESCE_S:
                    return held[1]  # repeat echo for the held unlock
                self._flush_unlock_echo()  # older distinct echo -> let it out
            self._unlock_echo = (ev, ctx_id, user_id)
            self._unlock_echo_cancel = async_call_later(
                self.hass, UNLOCK_COALESCE_S, self._flush_unlock_echo)
            return ctx_id
        # A real method DP: it wins outright.
        held = self._unlock_echo
        if held is not None:
            if abs(ev.dt - held[0].dt) <= UNLOCK_COALESCE_S:
                # Same unlock: drop the echo, adopt its (reserved) context.
                self._cancel_unlock_echo()
                ctx_id = held[1]
                if self._pending is held[0]:
                    self._pending = ev  # state not applied yet: richer reason
                self._emit_event("unlock", ev, cause_ctx_id=ctx_id, user_id=user_id)
                self._refine_unlock(ctx_id, ev)  # state already applied
                return ctx_id
            self._flush_unlock_echo()   # older distinct echo -> let it out
        self._emit_event("unlock", ev, cause_ctx_id=ctx_id, user_id=user_id)
        return ctx_id

    @callback
    def _flush_unlock_echo(self, _now=None) -> None:
        self._unlock_echo_cancel = None
        pending = self._unlock_echo
        if pending is None:
            return
        self._unlock_echo = None
        ev, ctx_id, user_id = pending
        self._emit_event("unlock", ev, cause_ctx_id=ctx_id, user_id=user_id)

    @callback
    def _cancel_unlock_echo(self) -> None:
        if self._unlock_echo_cancel is not None:
            self._unlock_echo_cancel()
            self._unlock_echo_cancel = None
        self._unlock_echo = None

    # -- HA unlock echo absorption ---------------------------------------------
    def _ha_echo(self, ev: LockEvent) -> _HaUnlock | None:
        """The HA unlock whose own echo ``ev`` is, if any: a live remote-type
        unlock reported while the command is in flight or within
        HA_ECHO_GRACE_S after it returned. Buffered replays (the wake can flush
        history) predate the command and are never absorbed."""
        rec = self._ha_unlock
        if (rec is None or ev.type != "unlock" or ev.method not in HA_ECHO_METHODS
                or not ev.fresh or ev.dt < rec.start_t - DOOR_CLOCK_SLACK_S):
            return None
        if rec.end_t is not None and time.time() > rec.end_t + HA_ECHO_GRACE_S:
            return None
        return rec

    @callback
    def _emit_ha_unlock(self, rec: _HaUnlock) -> None:
        """Emit the HA unlock's single activity line under its reserved ctx
        (idempotent). Used when the lock's response is the first evidence —
        before the command returns, or a late confirmation of a command that
        reported failure — so the state that parents to ``rec.ctx_id`` always
        has its line."""
        if rec.emitted:
            return
        rec.emitted = True
        rec.ev = LockEvent(type="unlock", dt=time.time(), source="command",
                           method="remote", user_id=rec.user_id)
        self._emit_event("unlock", rec.ev, cause_ctx_id=rec.ctx_id,
                         user_id=rec.user_id)

    def _unlock_near(self, dt: float):
        """The (ev, ctx_id, user_id) of the held or last-emitted unlock event for
        the physical unlock at device-time ``dt`` (within UNLOCK_COALESCE_S)."""
        for cand in (self._unlock_echo, self._last_unlock):
            if cand is not None and abs(cand[0].dt - dt) <= UNLOCK_COALESCE_S:
                return cand
        return None

    # -- auto-relock timer ----------------------------------------------------
    @callback
    def _start_relock(self) -> None:
        self._cancel_relock()
        if self._relock_seconds > 0:
            self._relock_cancel = async_call_later(
                self.hass, self._relock_seconds, self._relock_fired)

    @callback
    def _cancel_relock(self) -> None:
        if self._relock_cancel is not None:
            self._relock_cancel()
            self._relock_cancel = None

    @callback
    def _cancel_device_debounce(self) -> None:
        if self._device_debounce is not None:
            self._device_debounce()
            self._device_debounce = None

    @callback
    def _relock_fired(self, _now) -> None:
        self._relock_cancel = None
        if self._door_open():
            return  # door-open suppresses the timed relock
        if self._state == LOCKED:
            return  # already locked: nothing to assume (and no activity line)
        self._last_applied_dt = time.time()
        ev = LockEvent(type="state_report", dt=time.time(), source="timer", locked=True)
        self._transition(LOCKED, activity.lock_message("timer"), inferred=True,
                         cause_event=ev, discrete="lock")

    def _door_open(self) -> bool:
        if not self._door_entity:
            return False
        st = self.hass.states.get(self._door_entity)
        return st is not None and st.state == "on"  # door device_class: on=open

    # -- raw input (sources) --------------------------------------------------
    @callback
    def _on_raw(self, ev: LockEvent) -> None:
        if not isinstance(ev, LockEvent):
            return
        if ev.type == "connectivity":
            self._set_online(bool(ev.value))
            return
        if ev.source == "device" and not self._online:
            # The lock is evidently talking again (a DP frame), whether or not
            # the hub's presence frame has arrived yet.
            self._set_online(True)
        if ev.type in ("state_report", "unlock"):
            self._on_push_state(ev)
        elif ev.type in ("failed_unlock", "tamper"):
            # Non-state activity: no transition, just an activity event with its
            # own root context (no HA user).
            cause = Context()
            self._emit_event(ev.type, ev, cause_ctx_id=cause.id, user_id=None)

    @callback
    def _set_online(self, online: bool) -> None:
        """Track the hub's view of the lock's link (transitions only — the hub
        repeats presence, and protocol:25 + protocol:1 report the same change).

        Offline is surfaced as the lock entity going UNAVAILABLE, not as a kept
        state with a flag: actions at the door while offline are neither
        reported nor buffered (a later sync returns nothing), so the last known
        "locked" is exactly the confident-but-wrong answer to avoid; HA also
        refuses service calls to an unavailable entity, which matches reality
        (a cloud unlock can't reach an offline lock). The Sync button stays
        available to retry. Each transition gets an activity line ("Lock
        offline (state unknown)" / "Lock back online") that the entity's
        unavailable / restored row (and the connectivity sensor's row) parents
        to.

        On reconnect the state is UNKNOWN, not the last known one: whatever
        happened at the door during the outage was lost, so "locked" may be
        wrong. A sync is forced, and the next real event (device lock/unlock
        DP, door change, HA command) resolves it as usual. When the reconnect
        is implied by a device DP frame, _on_raw calls this BEFORE processing
        that DP, so the DP's own state lands after (not under) the unknown.

        Nothing armed before the outage may re-impose a stale state afterwards:
          - a debounced device state (``_apply``) still pending at the drop is
            flushed immediately — it's a real pre-outage report, so its row
            lands before the outage rather than after the reconnect;
          - the auto-relock failsafe is cancelled on the drop: it's an
            inference from the pre-outage unlock, and firing it while offline
            (or after, onto unknown) would fabricate an "Auto-locked" line;
          - a door hold is left to resolve on its timer: it only emits the
            unlock's ATTRIBUTION line (a flush may still arrive after the
            reconnect), never a state — the reconnect clears the refinable
            ctx, so its _refine_unlock no-ops."""
        if online == self._online:
            return
        if not online:
            # Settle the pre-outage report while the entity is still available.
            if self._device_debounce is not None:
                self._cancel_device_debounce()
                self._apply()
            self._cancel_relock()
        self._online = online
        _LOGGER.debug("lock link -> %s", "online" if online else "OFFLINE")
        ev = LockEvent(type="connectivity", dt=time.time(), source="hub",
                       subtype="online" if online else "offline", value=online)
        ctx_id = Context().id
        self._emit_event("connectivity", ev, cause_ctx_id=ctx_id, user_id=None)
        if online:
            # Defensive: nothing from before the outage survives the reconnect.
            self._cancel_relock()
            self._cancel_device_debounce()
            self._pending = None
            self._pending_ctx_id = None
            self._attributable_ctx_id = None
            self._state = None
            self._logged_state = None
            self._changed_by = activity.ONLINE_UNKNOWN
            self._inferred = True
            if self.coordinator.wake is not None:
                self.coordinator.wake.on_reconnect()
        self._publish_state(ctx_id, None)
        async_dispatcher_send(self.hass, signal_link(self.entry_id), {
            "online": online, "cause_ctx_id": ctx_id})

    @callback
    def _on_push_state(self, ev: LockEvent) -> None:
        # Auto-discover a newly-seen fingerprint/card id (fresh or buffered), so
        # the user only has to label it. Runs before the ordering guard so even a
        # superseded flush event registers its credential.
        if ev.type == "unlock" and ev.method in CREDENTIAL_METHODS:
            self._maybe_discover_credential(ev.method, ev.cred)

        # Emit the discrete activity event NOW — decoupled from the coalesced
        # state — so every unlock/lock is surfaced even when a buffered flush
        # nets it away (unlock -> relock settles on locked, but both happened).
        # Remember its context so, if this event wins the debounce, the
        # resulting state change is attributed to it. The event entity de-dupes
        # re-flushes by device-time.
        ctx_id: str | None = None
        hold = self._door_hold
        if ev.type == "unlock":
            _label, uid, _name = lookup(
                self._options.get(CONF_CREDENTIALS, []), ev.method, ev.cred)
            ev_user = ev.user_id or uid
            ha = self._ha_echo(ev)
            echo_of_emitted = (
                ev.method == "remote" and self._last_unlock_discrete_dt is not None
                and abs(ev.dt - self._last_unlock_discrete_dt) <= UNLOCK_COALESCE_S)
            if ha is not None:
                # The lock's response to OUR unlock command (dp61 echo / dp62
                # phone_remote, either order, before or after the command
                # returned): not a second unlock. It links to the HA unlock's
                # reserved ctx. A late confirmation of a command that reported
                # failure is the HA unlock landing after all: log it now.
                ha.echoed = True
                if ha.end_t is not None:
                    self._emit_ha_unlock(ha)
                ctx_id = ha.ctx_id
            elif hold is not None and hold.accepts(ev) and not echo_of_emitted:
                # A (buffered) credential unlock that may explain a held
                # door-open: collect it; the hold emits ONE unlock on resolution
                # under its reserved context.
                hold.candidates.append((ev, ev_user))
                ctx_id = hold.ctx_id
            else:
                # Coalesce the dp61 "remote" echo with its method DP so one
                # physical unlock yields exactly one discrete event.
                ctx_id = self._emit_unlock_discrete(
                    ev, Context(user_id=ev_user).id, ev_user)
        elif ev.type == "state_report" and ev.locked:
            if hold is not None:
                if self._lock_after_open(hold, ev):
                    # The door was unlocked (and opened) before this lock-up:
                    # settle the held unlock FIRST so its line precedes this one.
                    self._resolve_door_hold(by_lock=True)
                elif hold.in_window(ev.dt):
                    # A buffered relock from before the open: it disqualifies
                    # the credentials before it, which can be logged now (ahead
                    # of this lock line, in device order).
                    hold.lock_dts.append(ev.dt)
                    self._emit_held_unlocks(
                        hold, [c for c in hold.candidates if c[0].dt < ev.dt])
            ctx_id = Context().id
            self._emit_event("lock", ev, cause_ctx_id=ctx_id, user_id=None)

        # Immediate side-effects for FRESH (live) events only.
        if ev.fresh:
            if ev.type == "unlock":
                self._start_relock()  # (re)start the failsafe window
                if self.coordinator.wake is not None:
                    self.coordinator.wake.on_unlock()  # poll for the real auto-lock
            elif ev.type == "state_report" and ev.locked:
                self._cancel_relock()
                if self.coordinator.wake is not None:
                    # real lock seen: stop polling for it, and a pending
                    # post-close re-sync (looking for this lock-up) is moot.
                    self.coordinator.wake.cancel_polls()
                    self.coordinator.wake.cancel_followup()

        # Ordered + debounced state application: the latest device-time wins, so a
        # buffered flush settles on the lock's real final state (no flicker, and
        # stale replays don't fire live side-effects or replay old causes).
        if ev.dt <= self._last_applied_dt:
            return
        self._last_applied_dt = ev.dt
        self._pending = ev
        self._pending_ctx_id = ctx_id
        if self._device_debounce is not None:
            self._device_debounce()
        self._device_debounce = async_call_later(self.hass, DEVICE_DEBOUNCE_S, self._apply)

    @callback
    def _apply(self, _now=None) -> None:
        self._device_debounce = None
        ev = self._pending
        if ev is None:
            return
        self._pending = None
        cause_ctx_id = self._pending_ctx_id  # the discrete event emitted on arrival
        self._pending_ctx_id = None
        new = LOCKED if ev.locked else UNLOCKED
        if new == self._state:
            return  # no state change (the discrete event was already emitted)
        cause = ev
        if new == UNLOCKED and ev.type != "unlock":
            # A bare dp11:false won the ordering, but the unlock it reports has
            # its own (method/echo) event: attribute + link to THAT event.
            near = self._unlock_near(ev.dt)
            if near is not None:
                cause, cause_ctx_id = near[0], near[1]
        ha = self._ha_unlock
        if (new == UNLOCKED and ha is not None and cause_ctx_id is not None
                and cause_ctx_id == ha.ctx_id):
            # Our echo won the ordering (possibly while the command is still in
            # flight): the state is the HA unlock's, by its user.
            self._emit_ha_unlock(ha)
            cause = ha.ev
        user_id: str | None = None
        refinable = False
        if ev.fresh:
            if new == UNLOCKED:
                reason, display, user_id = self._attribute_unlock(cause)
                if ha is not None and cause is ha.ev:
                    display = ha.user_name
                self._last_method = cause.method
                self._last_user = display
                refinable = cause_ctx_id is not None
            else:
                self._cancel_relock()
                reason = activity.lock_message(ev.source)
        else:
            # buffered history: sync to the lock's real final state, don't replay
            if new == LOCKED:
                self._cancel_relock()
            reason = f"Synced from lock: {'locked' if new == LOCKED else 'unlocked'}"
        # The discrete event was already published in _on_push_state; link this
        # state change to it (cause_ctx_id) rather than emitting again.
        self._transition(new, reason, user_id=user_id, cause_ctx_id=cause_ctx_id)
        if refinable:
            self._attributable_ctx_id = cause_ctx_id

    def _attribute_unlock(
        self, ev: LockEvent
    ) -> tuple[str, str | None, str | None]:
        """Resolve an unlock event to (reason, display_user, user_id).

        The reason is the same rich line the activity log shows (see
        activity.unlock_message). When the credential maps to an HA user (or the
        event carries one, e.g. an HA command), ``user_id`` is returned so the
        state change is attributed to them ("By <user>")."""
        label, uid, user_name = lookup(
            self._options.get(CONF_CREDENTIALS, []), ev.method, ev.cred)
        reason = activity.unlock_message(
            ev.method, ev.cred, source=ev.source, label=label,
            user_name=user_name, user_mapped=bool(uid), who=ev.who)
        display = activity.unlock_actor(
            ev.method, ev.cred, label=label, user_name=user_name,
            user_mapped=bool(uid), who=ev.who)
        return reason, display, uid or ev.user_id

    @callback
    def _maybe_discover_credential(self, method: str, cred) -> None:
        """Auto-add a newly-seen credential id to the roster (blank label) and
        raise a persistent notification, so the user only has to fill in the
        label and (optionally) map it to an HA user. Credentials-only option
        writes don't reload the entry (see __init__._async_update_listener)."""
        cred_i = as_int(cred)
        if cred_i is None:
            return
        entry = self.entry
        creds = list(entry.options.get(CONF_CREDENTIALS, []))
        if any(c.get("method") == method and as_int(c.get("id")) == cred_i
               for c in creds):
            return  # already known
        creds.append({"method": method, "id": cred_i, "label": ""})
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_CREDENTIALS: creds})
        _LOGGER.info("Discovered new %s credential #%s", method, cred_i)
        persistent_notification.async_create(
            self.hass,
            f"A new **{method}** credential (#{cred_i}) just unlocked the ERA "
            f"Protect Lock. Add a label and (optionally) map it to a Home "
            f"Assistant user in the integration's options "
            f"(Settings → Devices & Services → ERA Protect Lock → Configure → "
            f"Unlock credentials).",
            title="New lock credential detected",
            notification_id=f"{DOMAIN}_new_cred_{method}_{cred_i}",
        )

    # -- door contact (Layer-1 source handled inline) -------------------------
    @callback
    def _on_door(self, event: Event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or new.state not in ("on", "off"):
            return
        # Only a real open/close transition counts. The sensor's first state
        # write at startup (or recovery from unavailable/unknown) isn't the door
        # moving — treating it as a close set HA to "unlocked" on every boot.
        if old is None or old.state not in ("on", "off"):
            return
        opened_from_locked = new.state == "on" and self._state == LOCKED
        if self.coordinator.wake is not None:
            # door change -> fetch lock state. Opening from locked forces the
            # wake past the battery rate limit: the held attribution below
            # depends on the buffered flush it triggers.
            self.coordinator.wake.on_door_change(force=opened_from_locked)
        self._last_applied_dt = time.time()
        if new.state == "on":  # opened -> can't be deadbolt-locked
            self._cancel_relock()
            if opened_from_locked:
                # Unlocked for certain, but HOW is ambiguous: a fingerprint/card
                # unlock may still be buffered in the lock. Go UNLOCKED now and
                # hold the attribution for the flush (see DOOR_HOLD_S); the state
                # change parents to the hold's reserved context, which the
                # eventual unlock event (credential, else thumbturn) is emitted
                # under.
                self._resolve_door_hold()  # settle any earlier hold first
                # The post-close re-sync is armed only once the hold resolves
                # as a thumbturn exit (see _resolve_door_hold).
                self._exit_pending = False
                self._last_method = None   # unknown until the hold resolves
                self._last_user = None
                hold = _DoorHold(Context().id, time.time())
                hold.cancel = async_call_later(
                    self.hass, DOOR_HOLD_S, self._resolve_door_hold)
                self._door_hold = hold
                self._transition(UNLOCKED, "Door opened (syncing with lock)",
                                 inferred=True, cause_ctx_id=hold.ctx_id)
                self._attributable_ctx_id = hold.ctx_id
            else:
                self._exit_pending = False
                if self._door_hold is not None:
                    self._door_hold.closed_t = None  # reopened before resolving
                if self._state != UNLOCKED:
                    ev = LockEvent(type="door", dt=time.time(), source="door",
                                   subtype="open")
                    self._transition(UNLOCKED, "Door opened", inferred=True,
                                     cause_event=ev, discrete="door")
        elif new.state == "off":  # closed: closing can't throw the deadbolt, so
            # assume the lock stays unlocked; a genuine handle-lift re-lock
            # arrives as dp11 on the wake this close triggers. Don't clobber a
            # more specific unlock reason (e.g. thumbturn) already set.
            self._cancel_relock()
            # Only resolve an UNKNOWN state here. Never override LOCKED: the door
            # can't shut with the bolts thrown, so a lock reported while the
            # door was "open" (stale state / mis-synced sensor) is the device's
            # word and wins over the inference.
            if self._state is None:
                ev = LockEvent(type="door", dt=time.time(), source="door",
                               subtype="close")
                self._transition(UNLOCKED, "Door closed", inferred=True,
                                 cause_event=ev, discrete="door")
            if self._door_hold is not None:
                # Closed before the hold resolved: whether this was an exit
                # isn't known yet — _resolve_door_hold decides (relative to now).
                self._door_hold.closed_t = time.time()
            elif self._exit_pending:
                self._exit_pending = False
                self._schedule_exit_resync(time.time())

    @callback
    def _schedule_exit_resync(self, closed_t: float) -> None:
        """locked -> thumbturn-open -> close: somebody likely left, and a
        lock-up tends to happen in the first minute. Re-sync POST_CLOSE_SYNC_S
        after the close to catch it (the immediate close wake fires too early).
        Only for a thumbturn EXIT — a credential open is someone coming in."""
        if self.coordinator.wake is not None:
            self.coordinator.wake.schedule_followup(
                max(0.0, closed_t + POST_CLOSE_SYNC_S - time.time()))

    def _lock_after_open(self, hold: _DoorHold, ev: LockEvent) -> bool:
        """Is a device lock reported during ``hold`` a lock-up AFTER the open
        (vs a buffered relock from before it)? After, if its device-time is at
        or past the open; once the door has closed again, also within the clock
        slack before it (the device clock may lag HA's) — a lock-up after the
        close is then far likelier than a relock seconds before the open."""
        if ev.dt >= hold.open_t:
            return True
        return (hold.closed_t is not None
                and ev.dt >= hold.open_t - DOOR_CLOCK_SLACK_S)

    @callback
    def _emit_held_unlocks(
        self, hold: _DoorHold, cands: list[tuple[LockEvent, str | None]],
    ) -> None:
        """Emit collected credential unlocks that do NOT explain the open (each
        a genuine earlier unlock, with its own context), in device order, and
        drop them from the hold. A dp61 echo of a collected method DP is
        dropped silently."""
        everything = list(hold.candidates)
        for ev, user in sorted(cands, key=lambda c: c[0].dt):
            if ev.method != "remote" or not any(
                    o is not ev and o.method != "remote"
                    and abs(o.dt - ev.dt) <= UNLOCK_COALESCE_S
                    for o, _u in everything):
                self._emit_event("unlock", ev,
                                 cause_ctx_id=Context(user_id=user).id,
                                 user_id=user)
        drop = {id(c[0]) for c in cands}
        hold.candidates = [c for c in hold.candidates if id(c[0]) not in drop]

    @callback
    def _resolve_door_hold(self, _now=None, *, by_lock: bool = False) -> None:
        """Settle a held door-open: emit ONE unlock under the hold's context —
        the credential unlock that explains it, else the thumbturn. Credential
        unlocks collected but not chosen (disqualified by an intervening device
        lock) are genuine earlier unlocks and are emitted as their own events,
        first.

        ``by_lock``: resolved early by a device lock-up after the open (the
        caller emits that lock's line next). A thumbturn result otherwise means
        someone left: arm the post-close re-sync — timed from the close if the
        door already shut during the hold, else on the coming close. A lock-up
        already seen makes the re-sync moot."""
        hold = self._door_hold
        if hold is None:
            return
        self._door_hold = None
        if hold.cancel is not None:
            hold.cancel()
            hold.cancel = None
        winner = _pick_door_cause(hold.candidates, hold.lock_dts, hold.open_t)
        self._emit_held_unlocks(
            hold, [c for c in hold.candidates
                   if winner is None or c[0] is not winner[0]])
        if winner is not None:
            ev, user = winner
        else:
            ev, user = LockEvent(type="unlock", dt=hold.open_t, source="door",
                                 method="thumbturn"), None
        _LOGGER.debug("door hold resolved: %s%s", ev.method,
                      " (by lock-up)" if by_lock else "")
        self._emit_event("unlock", ev, cause_ctx_id=hold.ctx_id, user_id=user)
        self._refine_unlock(hold.ctx_id, ev)
        self._exit_pending = False
        if winner is None and not by_lock:
            if hold.closed_t is not None:
                self._schedule_exit_resync(hold.closed_t)
            else:
                self._exit_pending = True  # door still open: arm on the close

    # -- commands (called by the lock entity, which resolves the caller) ------
    # Both commands return the context id of the activity line they emitted, so
    # the lock entity parents its own command-completion state write to it.
    async def async_lock(self, user_id: str | None) -> str | None:
        # No hardware lock command (the lock auto-relocks), so this is a manual
        # "mark as locked" to assert the secured state the silent mechanical
        # thumbturn doesn't report.
        self._cancel_relock()
        self._last_applied_dt = time.time()
        ev = LockEvent(type="state_report", dt=time.time(), source="command",
                       locked=True)
        return self._transition(
            LOCKED, activity.lock_message("command"), inferred=True,
            user_id=user_id, cause_event=ev, discrete="lock")

    async def async_unlock(
        self, user_id: str | None, user_name: str | None,
    ) -> str | None:
        # Mark the unlock in flight BEFORE publishing: the listener hears the
        # lock's response (dp61 echo, dp62 phone_remote) on the same connection,
        # often before the command returns, and it must be absorbed as our echo
        # (see HA_ECHO_GRACE_S / _ha_echo) rather than logged as an app unlock.
        rec = _HaUnlock(Context(user_id=user_id).id, user_id,
                        user_name or "Home Assistant", time.time())
        self._ha_unlock = rec
        try:
            await self.hass.async_add_executor_job(self._unlock)
        except HomeAssistantError:
            # Unconfirmed — but if the lock already reported an unlock for this
            # command (e.g. dp62 seen, dp61 echo missed) it did unlock.
            if not rec.echoed:
                raise
            _LOGGER.debug("HA unlock unconfirmed but the lock reported it; "
                          "treating as unlocked")
        finally:
            rec.end_t = time.time()
        self._last_method = "remote"
        self._last_user = rec.user_name
        self._last_applied_dt = time.time()
        if rec.emitted:
            # Its line was already emitted (an echo applied the state first).
            self._transition(UNLOCKED, "Unlocked in Home Assistant",
                             user_id=user_id, cause_ctx_id=rec.ctx_id)
            ctx_id = rec.ctx_id
        else:
            rec.emitted = True
            rec.ev = LockEvent(type="unlock", dt=time.time(), source="command",
                               method="remote", user_id=user_id)
            ctx_id = self._transition(
                UNLOCKED, "Unlocked in Home Assistant", user_id=user_id,
                cause_event=rec.ev, discrete="unlock", cause_ctx_id=rec.ctx_id)
        self._start_relock()
        # Poll for the lock's own auto-lock (dp11) before the failsafe timer; we
        # know we unlocked, so arm the polls here rather than waiting on an
        # inbound unlock event (an HA unlock only echoes a bare dp11:false).
        if self.coordinator.wake is not None:
            self.coordinator.wake.on_unlock()
        await self.coordinator.async_request_refresh()
        return ctx_id

    def _unlock(self) -> None:
        # Executor thread. The unlock rides the persistent listener's MQTT
        # connection: the broker allows one connection per clientId (ours is a
        # fixed constant), so a second, short-lived CloudMqttLock client would
        # kick the listener off and be kicked back in a loop (~10s of churn and
        # lost inbound frames per unlock).
        listener = self.coordinator.listener
        if listener is None:
            # No persistent connection (not the case once set up): standalone.
            mgr = self.coordinator.mgr
            combined = self.coordinator.combined
            try:
                ok = CloudMqttLock(combined, mgr.mqtt_secrets()).unlock()
            except MqttUnlockError:
                _LOGGER.debug("MQTT unlock failed; refreshing Tuya session and retrying")
                mgr.refresh_tuya_session()
                ok = CloudMqttLock(combined, mgr.mqtt_secrets()).unlock()
        else:
            try:
                ok = listener.unlock()
            except MqttUnlockError as e:
                _LOGGER.debug("MQTT unlock failed (%s); refreshing the listener's "
                              "Tuya session and retrying", e)
                listener.refresh_session()
                try:
                    ok = listener.unlock()
                except MqttUnlockError as e2:
                    raise HomeAssistantError(f"Unlock failed: {e2}") from e2
        if not ok:
            raise HomeAssistantError("Unlock command sent but not confirmed by the lock.")
