"""The ERA Protect Lock integration.

Builds the runtime session manager + local-poll coordinator from the config
entry, then sets up the lock/sensor platforms. The config (service_config +
secrets) is merged with the code DEFAULTS into one dict each consumer reads from.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AUTO_RELOCK_SECONDS,
    CONF_ERA,
    CONF_SECRETS,
    CONF_SERVICE_CONFIG,
    CONF_SESSION,
    CONF_WAKE_HEARTBEAT,
    DEFAULT_AUTO_RELOCK_SECONDS,
    DEFAULT_WAKE_HEARTBEAT,
    PLATFORMS,
)
from .coordinator import EraLockCoordinator
from .eralock.secrets import DEFAULTS
from .eralock.session import Session
from .mqtt_listener import EraMqttListener
from .session import EraSessionManager
from .wake import EraWakeManager

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
    # Tolerant: a failed local poll leaves entities unavailable rather than
    # blocking setup (cloud unlock still works without the LAN).
    await coordinator.async_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start the persistent cloud-MQTT listener (push state + unlock events).
    listener = EraMqttListener(hass, mgr, combined, entry.entry_id)
    coordinator.listener = listener
    await hass.async_add_executor_job(listener.start)

    # Intelligent wake: pick up buffered events at worthwhile moments.
    wake = EraWakeManager(
        hass, listener,
        heartbeat_s=int(entry.options.get(CONF_WAKE_HEARTBEAT, DEFAULT_WAKE_HEARTBEAT)),
    )
    coordinator.wake = wake
    listener.set_wake_manager(wake)
    wake.start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: EraConfigEntry) -> None:
    """Reload the entry when options change (door contact / relock window)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EraConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data
    if coordinator.wake is not None:
        coordinator.wake.stop()
    if coordinator.listener is not None:
        await hass.async_add_executor_job(coordinator.listener.stop)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
