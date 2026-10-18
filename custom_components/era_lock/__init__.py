"""The ERA Protect Lock integration.

Builds the runtime session manager + local-poll coordinator from the config
entry, then sets up the lock/sensor platforms. The config (service_config +
secrets) is merged with the code DEFAULTS into one dict each consumer reads from.

Setup order (never half-set-up): controller (subscribed before anything can
emit) -> cloud MQTT listener, waited on until CONNECTED (a stale stored session
is re-minted on the way) -> platforms -> wake manager + one startup sync. The
controller is seeded with the last known lock state (helpers.storage) before it
starts, so the startup sync's replay corrects it through the normal paths. An
ERA account rejection raises ConfigEntryAuthFailed (HA starts reauth); no
connection within STARTUP_CONNECT_TIMEOUT_S raises ConfigEntryNotReady (HA
retries). The first LAN poll runs in the background, so a dead hub can't stall
setup.

Auth lifecycle at runtime: every successful re-mint is persisted to the entry's
data (no reload); a rejection stops the listener, makes the lock unavailable and
starts reauth once; the reauth flow's new credentials reload the entry.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CREDENTIALS,
    CONF_ERA,
    CONF_SECRETS,
    CONF_SERVICE_CONFIG,
    CONF_SESSION,
    DOMAIN,
    PLATFORMS,
    STARTUP_CONNECT_TIMEOUT_S,
    wake_heartbeat,
)
from .controller import STORAGE_VERSION, EraController
from .coordinator import EraLockCoordinator, entry_credentials
from .eralock.secrets import DEFAULTS
from .eralock.session import Session
from .mqtt_listener import EraMqttListener
from .session import EraSessionManager
from .wake import EraWakeManager

_LOGGER = logging.getLogger(__name__)

type EraConfigEntry = ConfigEntry[EraLockCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EraConfigEntry) -> bool:
    """Set up ERA Protect Lock from a config entry."""
    d = entry.data
    combined = {**DEFAULTS, **d[CONF_SERVICE_CONFIG], **d[CONF_SECRETS]}
    mgr = EraSessionManager(
        sign_secrets=combined,
        email=d[CONF_ERA]["email"],
        refresh_token=d[CONF_ERA]["refresh_token"],
        session=Session.from_dict(d[CONF_SESSION]),
    )
    coordinator = EraLockCoordinator(hass, entry, mgr, combined)
    # Snapshot the reload-relevant options so a later credentials-only change
    # (auto-discovery, label/user edits) doesn't needlessly reload the entry.
    coordinator.options_snapshot = _reload_relevant(entry.options)
    entry.runtime_data = coordinator

    # Persist every successful re-mint (paho / executor thread -> HA loop).
    mgr.on_credentials_changed = lambda creds: hass.loop.call_soon_threadsafe(
        coordinator.async_persist_credentials, creds)

    # Layer 1: the persistent cloud-MQTT listener (normalises frames -> signal_raw).
    listener = EraMqttListener(hass, mgr, combined, entry.entry_id)
    listener.on_auth_failed = coordinator.async_handle_auth_failed
    coordinator.listener = listener

    # Intelligent wake: pick up buffered events at worthwhile moments.
    wake = EraWakeManager(hass, listener, heartbeat_s=wake_heartbeat(entry.options))
    coordinator.wake = wake
    listener.set_wake_manager(wake)

    # Layer 2: the controller owns all decision logic. Seed it with the state
    # from before the restart, then start it (subscribe to signal_raw + the
    # door entity) — both before the listener begins emitting, so the startup
    # sync's buffered replay lands on top of the restored state in order.
    controller = EraController(hass, entry, coordinator)
    coordinator.controller = controller
    await controller.async_restore()
    controller.start()
    entry.async_on_unload(controller.stop)

    try:
        # Connect before creating entities: a dead or rejected session must
        # not leave entities that silently never update.
        await hass.async_add_executor_job(listener.start)
        outcome = await hass.async_add_executor_job(
            listener.wait_first_outcome, STARTUP_CONNECT_TIMEOUT_S)
        if outcome == "auth_failed":
            raise ConfigEntryAuthFailed(
                f"ERA account rejected the stored credentials: {listener.auth_error}")
        if outcome != "connected":
            raise ConfigEntryNotReady(
                f"ERA cloud connection not established within "
                f"{STARTUP_CONNECT_TIMEOUT_S:.0f}s")

        # Tolerant + non-blocking: a failed/slow local poll only leaves the
        # battery to MQTT (cloud unlock and lock state don't use the LAN).
        entry.async_create_background_task(
            hass, coordinator.async_refresh(), "era_lock first LAN poll")

        # Layer 3: the entities (lock/sensor/button/event).
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Never leave our fixed-clientId MQTT session running behind a failed
        # setup (HA's retry would fight it).
        coordinator.closing = True
        await hass.async_add_executor_job(listener.stop)
        raise

    # Restart the listener if its paho network thread ever dies.
    entry.async_on_unload(listener.async_start_watchdog())
    wake.start()
    coordinator.started = True
    # The lock may be holding buffered events (and the state is only the
    # restored, inferred one — or unknown): one forced sync now that we're
    # connected.
    wake.startup_sync()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    stop_fired = False

    async def _async_on_stop(_event: Event) -> None:
        # HA doesn't unload entries on shutdown: close the MQTT session cleanly.
        nonlocal stop_fired
        stop_fired = True
        await _async_shutdown(hass, coordinator)

    unsub_stop = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop)

    def _remove_stop_listener() -> None:
        # A fired one-time listener is already gone; removing it again logs.
        if not stop_fired:
            unsub_stop()

    entry.async_on_unload(_remove_stop_listener)
    return True


async def _async_shutdown(hass: HomeAssistant, coordinator: EraLockCoordinator) -> None:
    """Stop waking and close the MQTT session (unload or HA stop; idempotent)."""
    coordinator.closing = True
    if coordinator.wake is not None:
        coordinator.wake.stop()
    if coordinator.listener is not None:
        await hass.async_add_executor_job(coordinator.listener.stop)
    # After the listener: nothing can change the state any more. Written now so
    # a reload's new controller restores it (not a pending delayed save).
    if coordinator.controller is not None:
        await coordinator.controller.async_persist()


def _reload_relevant(options: dict) -> dict:
    """Options whose change requires a reload — everything except the credential
    roster, which the entities read live."""
    return {k: v for k, v in options.items() if k != CONF_CREDENTIALS}


async def _async_update_listener(hass: HomeAssistant, entry: EraConfigEntry) -> None:
    """Reload the entry when reload-relevant options change (door contact, relock
    window, heartbeat) or when its credentials were replaced from outside — the
    reauth flow. Two kinds of change are applied live instead:
      - a credentials-only OPTIONS change (auto-discovery, a label/user edit):
        read live by the entities; reloading would reset the state right after
        an unlock;
      - our own persistence of a re-minted session (the DATA matches
        ``persisted_creds``): the running session manager already holds it."""
    coordinator = entry.runtime_data
    current = _reload_relevant(entry.options)
    if (getattr(coordinator, "options_snapshot", None) == current
            and entry_credentials(entry.data)
            == getattr(coordinator, "persisted_creds", None)):
        return
    coordinator.options_snapshot = current
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EraConfigEntry) -> bool:
    """Unload a config entry."""
    await _async_shutdown(hass, entry.runtime_data)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EraConfigEntry) -> None:
    """The entry was deleted: drop the lock state stored for restarts."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
