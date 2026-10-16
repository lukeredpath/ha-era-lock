"""Decision layer (Layer 2) for the ERA Protect Lock.

The controller owns ALL logic. It subscribes to ``signal_raw`` (normalised
:class:`LockEvent`s from the sources) and to the optional door-contact entity,
runs the inferred-state machine (device-time ordering + debounce, fresh-vs-stale
handling, auto-relock failsafe, credential auto-discovery, attribution), drives
the wake manager, and performs the cloud unlock command. It emits two outputs:

  - ``signal_event``  enriched discrete activity events for the event entity;
  - ``signal_state``  state-model updates the lock entity renders verbatim.

Attribution linkage: every state transition caused by a discrete event mints a
single ``Context`` (``cause_ctx``). The discrete event is published with that
context's id as its own id (it becomes the activity-log root cause row); the
state update is published with that id as a parent, so the lock state change is
shown "triggered by" its event and attributed to the right HA user.

This is a behaviour-preserving port of the old ``EraLockEntity`` state machine;
semantics are unchanged except for the added event-entity output and context
threading.
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

from .attribution import actor as attr_actor
from .attribution import as_int, lookup
from .const import (
    CAUSE_SOURCE,
    CONF_AUTO_RELOCK_SECONDS,
    CONF_CREDENTIALS,
    CONF_DOOR_CONTACT_ENTITY_ID,
    CREDENTIAL_METHODS,
    DEFAULT_AUTO_RELOCK_SECONDS,
    DOMAIN,
    EVENT_CAUSE,
)
from .eralock.mqtt import CloudMqttLock, MqttUnlockError
from .events import LockEvent, signal_event, signal_raw, signal_state

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import EraLockCoordinator

_LOGGER = logging.getLogger(__name__)

LOCKED = "locked"
UNLOCKED = "unlocked"
DEVICE_DEBOUNCE_S = 2.0  # coalesce dp11 mini-history bursts into one transition


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
        self._exit_pending: bool = False  # locked->thumbturn-open awaiting a close
        self._relock_cancel = None
        self._logged_state: str | None = "__init__"  # cause/logbook de-dupe
        # -- subscriptions ----------------------------------------------------
        self._unsub_raw = None
        self._unsub_door = None

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

    # -- transition + outputs -------------------------------------------------
    @callback
    def _transition(
        self, state: str | None, reason: str, *, inferred: bool = False,
        user_id: str | None = None, cause_event: LockEvent | None = None,
        discrete: str | None = None, cause_ctx_id: str | None = None,
    ) -> None:
        """Change state and attribute it to the event that caused it.

        Pass EITHER ``cause_ctx_id`` — when the discrete activity event was
        already published on arrival (listener-sourced, via _on_push_state) — OR
        ``discrete`` + ``cause_event`` for a direct transition (command, door,
        timer) whose discrete event is published here. One cause context is shared
        by the discrete event, the EVENT_CAUSE logbook row, and the state change,
        so the activity log shows the state change "triggered by" its event (and
        "By <user>"). ``inferred`` marks a state HA deduced (door sensor, relock
        timer, manual assertion). The context is minted only when the state
        actually changes (de-dupe by ``_logged_state``)."""
        self._changed_by = reason
        self._inferred = inferred
        _LOGGER.debug("lock -> %s  (%s)%s", state or "unknown", reason,
                      " [inferred]" if inferred else "")
        if state != self._logged_state:
            self._logged_state = state
            if cause_ctx_id is None:
                cause_ctx_id = Context(user_id=user_id).id
            if discrete and cause_event is not None:
                self._emit_event(discrete, cause_event,
                                 cause_ctx_id=cause_ctx_id, user_id=user_id)
            self.hass.bus.async_fire(
                EVENT_CAUSE, {"name": CAUSE_SOURCE, "message": reason},
                context=Context(id=cause_ctx_id, user_id=user_id))
        else:
            cause_ctx_id = None  # repeat of the same logged state: no new cause
        self._state = state
        async_dispatcher_send(self.hass, signal_state(self.entry_id), {
            "state": self._state,
            "changed_by": self._changed_by,
            "inferred": self._inferred,
            "last_method": self._last_method,
            "last_user": self._last_user,
            "cause_ctx_id": cause_ctx_id,
            "user_id": user_id,
        })

    @callback
    def _emit_event(
        self, etype: str, ev: LockEvent, *, cause_ctx_id: str | None,
        user_id: str | None,
    ) -> None:
        """Publish an enriched discrete activity event. Attribution (label/user/
        actor) is resolved here so the event entity needs no options access."""
        creds = self._options.get(CONF_CREDENTIALS, [])
        label, mapped_uid, user_name = lookup(creds, ev.method, ev.cred)
        # Only name an actor when there's a real one to name; a plain
        # lock/failed_unlock/tamper/door carries no credential, so "unknown"
        # is dropped to None rather than surfaced as a spurious attribute.
        actor = attr_actor(creds, ev.method, ev.cred, ev.who)
        if actor == "unknown":
            actor = None
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
            "cause_ctx_id": cause_ctx_id,
        })

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
        self._last_applied_dt = time.time()
        ev = LockEvent(type="state_report", dt=time.time(), source="timer", locked=True)
        self._transition(LOCKED, "Auto-lock (timer)", inferred=True,
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
        if ev.type in ("state_report", "unlock"):
            self._on_push_state(ev)
        elif ev.type in ("failed_unlock", "tamper"):
            # Non-state activity: no transition, just an activity event with its
            # own root context (no HA user).
            cause = Context()
            self._emit_event(ev.type, ev, cause_ctx_id=cause.id, user_id=None)

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
        # Mint its context and remember it so, if this event wins the debounce,
        # the resulting state change is attributed to it. The event entity
        # de-dupes re-flushes by device-time.
        ctx_id: str | None = None
        if ev.type == "unlock":
            _label, uid, _name = lookup(
                self._options.get(CONF_CREDENTIALS, []), ev.method, ev.cred)
            ev_user = ev.user_id or uid
            ctx_id = Context(user_id=ev_user).id
            self._emit_event("unlock", ev, cause_ctx_id=ctx_id, user_id=ev_user)
        elif ev.type == "state_report" and ev.locked:
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
                    self.coordinator.wake.cancel_polls()  # real lock seen; stop polling

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
        user_id: str | None = None
        if ev.fresh:
            if new == UNLOCKED:
                reason, display, user_id = self._attribute_unlock(
                    ev.method, ev.cred, ev.who)
                self._last_method = ev.method
                self._last_user = display
            else:
                self._cancel_relock()
                reason = "Device Locked"
        else:
            # buffered history: sync to the lock's real final state, don't replay
            if new == LOCKED:
                self._cancel_relock()
            reason = f"Synced from lock: {'locked' if new == LOCKED else 'unlocked'}"
        # The discrete event was already published in _on_push_state; link this
        # state change to it (cause_ctx_id) rather than emitting again.
        self._transition(new, reason, user_id=user_id, cause_ctx_id=cause_ctx_id)

    def _attribute_unlock(
        self, method: str | None, cred, who: str | None
    ) -> tuple[str, str | None, str | None]:
        """Resolve an unlock credential to (reason, display_user, user_id).

        Priority: an options-configured credential — mapped HA user, else its
        custom label — then the frida roster name (``who``), then the raw
        method+id. When a credential maps to an HA user, ``user_id`` is returned
        so the state change is attributed to them ("By <user>") with the method."""
        label, uid, user_name = lookup(self._options.get(CONF_CREDENTIALS, []), method, cred)
        if uid:
            name = user_name or "a Home Assistant user"
            return f"Unlocked by {name} ({method})", name, uid
        if label:
            return f"Unlocked by {label} ({method})", label, None
        if who:
            return f"Unlocked by {who}", who, None
        cred_i = as_int(cred)
        if method:
            tag = f"{method} #{cred_i}" if cred_i is not None else method
            return f"Unlocked ({tag})", None, None
        return "Unlocked", None, None

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
        if new is None:
            return
        if self.coordinator.wake is not None:
            self.coordinator.wake.on_door_change()  # door change -> fetch lock state
        self._last_applied_dt = time.time()
        if new.state == "on":  # opened -> can't be deadbolt-locked
            self._cancel_relock()
            if self._state == LOCKED:
                # The deadbolt can't be thrown while the door opens, so opening
                # from a locked state means the internal thumbturn was used to
                # unlock — the one manual path the lock reports no DP for.
                self._last_method = "thumbturn"
                self._last_user = None
                self._exit_pending = True  # arm the post-close re-sync
                ev = LockEvent(type="unlock", dt=time.time(), source="door",
                               method="thumbturn")
                self._transition(UNLOCKED, "Unlocked (thumbturn)", inferred=True,
                                 cause_event=ev, discrete="unlock")
            else:
                self._exit_pending = False
                ev = LockEvent(type="door", dt=time.time(), source="door",
                               subtype="open")
                self._transition(UNLOCKED, "Door opened", inferred=True,
                                 cause_event=ev, discrete="door")
        elif new.state == "off":  # closed: closing can't throw the deadbolt, so
            # assume the lock stays unlocked; a genuine handle-lift re-lock
            # arrives as dp11 on the wake this close triggers. Don't clobber a
            # more specific unlock reason (e.g. thumbturn) already set.
            self._cancel_relock()
            if self._state != UNLOCKED:
                ev = LockEvent(type="door", dt=time.time(), source="door",
                               subtype="close")
                self._transition(UNLOCKED, "Door closed", inferred=True,
                                 cause_event=ev, discrete="door")
            if self._exit_pending:
                # locked -> thumbturn-open -> close: somebody likely left, and a
                # lock-up tends to happen in the first minute. Re-sync then to
                # catch it (the immediate close wake fires too early to see it).
                self._exit_pending = False
                if self.coordinator.wake is not None:
                    self.coordinator.wake.schedule_followup()

    # -- commands (called by the lock entity, which resolves the caller) ------
    async def async_lock(self, user_id: str | None) -> None:
        # No hardware lock command (the lock auto-relocks), so this is a manual
        # "mark as locked" to assert the secured state the silent mechanical
        # thumbturn doesn't report.
        self._cancel_relock()
        self._last_applied_dt = time.time()
        ev = LockEvent(type="state_report", dt=time.time(), source="command",
                       locked=True)
        self._transition(LOCKED, "Locked in Home Assistant", inferred=True,
                         user_id=user_id, cause_event=ev, discrete="lock")

    async def async_unlock(self, user_id: str | None, user_name: str | None) -> None:
        await self.hass.async_add_executor_job(self._unlock)
        self._last_method = "remote"
        self._last_user = user_name or "Home Assistant"
        self._last_applied_dt = time.time()
        ev = LockEvent(type="unlock", dt=time.time(), source="command",
                       method="remote", user_id=user_id)
        self._transition(UNLOCKED, "Unlocked in Home Assistant", user_id=user_id,
                         cause_event=ev, discrete="unlock")
        self._start_relock()
        # Poll for the lock's own auto-lock (dp11) before the failsafe timer; we
        # know we unlocked, so arm the polls here rather than waiting on an
        # inbound unlock event (an HA unlock only echoes a bare dp11:false).
        if self.coordinator.wake is not None:
            self.coordinator.wake.on_unlock()
        await self.coordinator.async_request_refresh()

    def _unlock(self) -> None:
        mgr = self.coordinator.mgr
        combined = self.coordinator.combined
        try:
            ok = CloudMqttLock(combined, mgr.mqtt_secrets()).unlock()
        except MqttUnlockError:
            _LOGGER.debug("MQTT unlock failed; refreshing Tuya session and retrying")
            mgr.refresh_tuya_session()
            ok = CloudMqttLock(combined, mgr.mqtt_secrets()).unlock()
        if not ok:
            raise HomeAssistantError("Unlock command sent but not confirmed by the lock.")
