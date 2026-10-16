"""Lock entity for the ERA Protect Lock.

Cloud-assisted unlock over Tuya MQTT (the proven path). The hardware reports no
authoritative lock state and relocks automatically, so state is inferred:

  - unlock (push event / command) -> `unlocked`, start the auto-relock timer;
  - timer elapses with the door still shut -> `locked`;
  - an optional door-contact sensor corrects the silent mechanical path: the
    door cannot open while deadbolt-locked, so a door **open** means `unlocked`
    (this catches a manual thumbturn), and after the door **closes** the state
    is `unknown` (a silent manual relock is unknowable) until the next event;
  - `lock()` is a manual "mark as locked" so the user can resolve `unknown`.

`assumed_state` is set, and the entity stays available without the local LAN
poll (unlock rides cloud MQTT).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EraConfigEntry
from .const import (
    CAUSE_SOURCE,
    CONF_AUTO_RELOCK_SECONDS,
    CONF_DOOR_CONTACT_ENTITY_ID,
    DEFAULT_AUTO_RELOCK_SECONDS,
    EVENT_CAUSE,
)
from .coordinator import EraLockCoordinator
from .entity import device_info
from .eralock.mqtt import CloudMqttLock, MqttUnlockError
from .mqtt_listener import signal_state

_LOGGER = logging.getLogger(__name__)

LOCKED = "locked"
UNLOCKED = "unlocked"
DEVICE_DEBOUNCE_S = 2.0  # coalesce dp11 mini-history bursts into one transition


async def async_setup_entry(
    hass: HomeAssistant, entry: EraConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([EraLockEntity(entry.runtime_data, entry)])


class EraLockEntity(CoordinatorEntity[EraLockCoordinator], LockEntity):
    """The door lock, with best-effort inferred state."""

    _attr_has_entity_name = True
    _attr_name = None  # main entity -> uses the device name
    _attr_assumed_state = True

    def __init__(self, coordinator: EraLockCoordinator, entry: EraConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{entry.unique_id}_lock"
        self._attr_device_info = device_info(entry)
        self._state: str | None = None
        self._relock_cancel = None
        self._changed_by: str | None = None
        self._last_method: str | None = None
        self._last_user: str | None = None
        self._logged_state: str | None = "__init__"  # for logbook de-dupe
        self._pending: dict | None = None
        self._device_debounce = None
        self._last_applied_dt: float = 0.0  # device-time of the last applied state
        self._unlocking: bool = False  # transient "Unlocking…" UI state
        self._inferred: bool = True  # True until the lock itself confirms a state
        self._exit_pending: bool = False  # locked->thumbturn-open awaiting a close

    # -- options --------------------------------------------------------------
    @property
    def _options(self) -> dict:
        return dict(self.coordinator.config_entry.options)

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
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # State starts unknown until an unlock event (dp11 is too ambiguous to
        # seed from); the fallback relock timer + mark-as-locked resolve it.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_state(self._entry_id), self._on_push_state)
        )
        if self._door_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._door_entity], self._on_door)
            )
        self.async_on_remove(self._cancel_relock)
        self.async_on_remove(self._cancel_device_debounce)

    @property
    def available(self) -> bool:
        # Unlock rides cloud MQTT, independent of the local LAN poll.
        return True

    @property
    def is_locked(self) -> bool | None:
        if self._state is None:
            return None
        return self._state == LOCKED

    @property
    def is_unlocking(self) -> bool:
        return self._unlocking

    @property
    def changed_by(self) -> str | None:
        """What last changed the lock (shown in the UI + logbook)."""
        return self._changed_by

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "reason": self._changed_by,
            "inferred": self._inferred,
            "last_unlock_method": self._last_method,
            "last_unlock_by": self._last_user,
        }

    @callback
    def _set(self, state: str | None) -> None:
        self._state = state
        self.async_write_ha_state()

    @callback
    def _transition(
        self, state: str | None, reason: str, *, inferred: bool = False
    ) -> None:
        """Change state; fire a cause event as the change's context parent so the
        activity log's "what happened" box shows the cause. ``inferred`` marks a
        state HA deduced (door sensor, relock timer, manual assertion) rather than
        one the lock itself reported."""
        self._changed_by = reason
        self._inferred = inferred
        _LOGGER.debug("lock -> %s  (%s)%s", state or "unknown", reason,
                      " [inferred]" if inferred else "")
        if state != self._logged_state:
            self._logged_state = state
            cause = Context()
            self.hass.bus.async_fire(
                EVENT_CAUSE, {"name": CAUSE_SOURCE, "message": reason},
                context=cause)
            self.async_set_context(Context(parent_id=cause.id))
        self._set(state)

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
        self._transition(LOCKED, "Auto-lock (timer)", inferred=True)

    def _door_open(self) -> bool:
        if not self._door_entity:
            return False
        st = self.hass.states.get(self._door_entity)
        return st is not None and st.state == "on"  # door device_class: on=open

    # -- inputs ---------------------------------------------------------------
    @callback
    def _on_push_state(self, payload) -> None:
        if not isinstance(payload, dict):
            return
        source = payload.get("source")
        method = payload.get("method")
        who = payload.get("who")
        dt = payload.get("dt", 0)
        fresh = payload.get("fresh", True)
        locked = payload.get("locked")

        # Immediate side-effects for FRESH (live) events only.
        if fresh:
            if source == "event" and not locked:
                self._start_relock()  # (re)start the failsafe window
                if self.coordinator.wake is not None:
                    self.coordinator.wake.on_unlock()  # poll for the real auto-lock
            elif source == "device" and locked:
                self._cancel_relock()
                if self.coordinator.wake is not None:
                    self.coordinator.wake.cancel_polls()  # real lock seen; stop polling

        # Ordered + debounced state application: the latest device-time wins, so a
        # buffered flush settles on the lock's real final state (no flicker, and
        # stale replays don't fire live side-effects or replay old causes).
        if dt <= self._last_applied_dt:
            return
        self._last_applied_dt = dt
        self._pending = payload
        if self._device_debounce is not None:
            self._device_debounce()
        self._device_debounce = async_call_later(self.hass, DEVICE_DEBOUNCE_S, self._apply)

    @callback
    def _apply(self, _now=None) -> None:
        self._device_debounce = None
        p = self._pending
        if p is None:
            return
        self._pending = None
        new = LOCKED if p.get("locked") else UNLOCKED
        if new == self._state:
            return  # no actual change
        if p.get("fresh"):
            if new == UNLOCKED:
                self._last_method = p.get("method")
                self._last_user = p.get("who")
                who, method = p.get("who"), p.get("method")
                reason = (f"Unlocked by {who}" if who
                          else f"Unlocked ({method})" if method else "Unlocked")
            else:
                self._cancel_relock()
                reason = "Device Locked"
        else:
            # buffered history: sync to the lock's real final state, don't replay
            if new == LOCKED:
                self._cancel_relock()
            reason = f"Synced from lock: {'locked' if new == LOCKED else 'unlocked'}"
        self._transition(new, reason)

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
                self._transition(UNLOCKED, "Unlocked (thumbturn)", inferred=True)
            else:
                self._exit_pending = False
                self._transition(UNLOCKED, "Door opened", inferred=True)
        elif new.state == "off":  # closed: closing can't throw the deadbolt, so
            # assume the lock stays unlocked; a genuine handle-lift re-lock
            # arrives as dp11 on the wake this close triggers. Don't clobber a
            # more specific unlock reason (e.g. thumbturn) already set.
            self._cancel_relock()
            if self._state != UNLOCKED:
                self._transition(UNLOCKED, "Door closed", inferred=True)
            if self._exit_pending:
                # locked -> thumbturn-open -> close: somebody likely left, and a
                # lock-up tends to happen in the first minute. Re-sync then to
                # catch it (the immediate close wake fires too early to see it).
                self._exit_pending = False
                if self.coordinator.wake is not None:
                    self.coordinator.wake.schedule_followup()

    # -- commands -------------------------------------------------------------
    async def async_lock(self, **kwargs: Any) -> None:
        # No hardware lock command (the lock auto-relocks), so this is a manual
        # "mark as locked" to assert the secured state the silent mechanical
        # thumbturn doesn't report.
        self._cancel_relock()
        self._last_applied_dt = time.time()
        self._transition(LOCKED, "Locked in Home Assistant", inferred=True)

    async def async_unlock(self, **kwargs: Any) -> None:
        # Show the transitioning ("Unlocking…") state immediately; the cloud
        # round-trip takes a second or two.
        self._unlocking = True
        self.async_write_ha_state()
        try:
            await self.hass.async_add_executor_job(self._unlock)
        except Exception:
            self._unlocking = False
            self.async_write_ha_state()
            raise
        self._unlocking = False
        self._last_method = "remote"
        self._last_user = "Home Assistant"
        self._last_applied_dt = time.time()
        self._transition(UNLOCKED, "Unlocked in Home Assistant")
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
