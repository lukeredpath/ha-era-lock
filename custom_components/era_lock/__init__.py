"""The ERA Protect Lock integration.

Builds the runtime session manager + local-poll coordinator from the config
entry, then sets up the lock/sensor platforms. The config (service_config +
secrets) is merged with the code DEFAULTS into one dict each consumer reads from.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ERA, CONF_SECRETS, CONF_SERVICE_CONFIG, CONF_SESSION, PLATFORMS
from .coordinator import EraLockCoordinator
from .eralock.secrets import DEFAULTS
from .eralock.session import Session
from .session import EraSessionManager

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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EraConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
