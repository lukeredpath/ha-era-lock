"""Local-poll data coordinator for the ERA Protect Lock.

Polls the hub over the LAN (tinytuya) for battery + the current datapoint
snapshot. Battery normally arrives over cloud MQTT (dp8, in the DP dump the lock
sends when it wakes — see ``async_set_battery``), so the LAN poll is only a slow
fallback (DEFAULT_SCAN_INTERVAL). If no hub_ip is configured, polling is skipped
and the battery comes from MQTT alone (cloud-only setup).

It is also the entry's runtime holder (listener / wake manager / controller) and
owns the auth lifecycle hooks: persisting re-minted credentials to the config
entry, and starting reauth once when the ERA account rejects them.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_ERA, CONF_SESSION, DEFAULT_SCAN_INTERVAL, DOMAIN
from .eralock.local import LocalLock
from .eralock.secrets import SecretsError
from .session import EraSessionManager

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


def entry_credentials(data) -> dict:
    """The mutable credential state held in a config entry's data, in the
    shape of ``EraSessionManager.entry_credentials()``."""
    return {"refresh_token": (data.get(CONF_ERA) or {}).get("refresh_token"),
            "session": dict(data.get(CONF_SESSION) or {})}


class EraLockCoordinator(DataUpdateCoordinator[dict]):
    """Coordinates local-LAN reads and holds the shared runtime objects."""

    def __init__(self, hass: HomeAssistant, entry: "ConfigEntry",
                 mgr: EraSessionManager, combined: dict) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.mgr = mgr
        self.combined = combined
        self.listener = None  # EraMqttListener, set during setup
        self.wake = None  # EraWakeManager, set during setup
        self.controller = None  # EraController, set during setup
        self._local = LocalLock(combined)
        # Battery: the most recent reading from either source.
        self._battery: int | None = None
        self.battery_source: str | None = None
        self.battery_updated: float | None = None
        # Reload-relevant options at setup (see __init__._async_update_listener).
        self.options_snapshot: dict | None = None
        # The credentials the entry's data holds as far as WE know: what setup
        # started from, then each re-mint we persisted. A data change that
        # doesn't match it came from elsewhere (reauth) and reloads the entry.
        self.persisted_creds: dict = entry_credentials(entry.data)
        self.auth_error: str | None = None
        self.started = False   # setup completed
        self.closing = False   # unloading / HA stopping: no more entry writes

    @property
    def has_local(self) -> bool:
        return bool(self.combined.get("hub_ip"))

    # -- battery --------------------------------------------------------------
    @property
    def battery(self) -> int | None:
        return self._battery

    @callback
    def async_set_battery(self, value: int, source: str = "mqtt") -> None:
        """A battery reading: MQTT dp8 (via the controller) or the LAN poll."""
        changed = value != self._battery
        self._battery = value
        self.battery_source = source
        self.battery_updated = time.time()
        if changed and source != "lan":
            self.async_update_listeners()  # a LAN poll notifies by itself

    # -- auth lifecycle (HA loop) -----------------------------------------------
    @callback
    def async_persist_credentials(self, creds: dict) -> None:
        """Write re-minted credentials (refresh token + Tuya session) to the
        config entry, so a restart starts from them rather than from the
        original session. Data-only: the update listener recognises it
        (``persisted_creds``) and does NOT reload."""
        entry = self.config_entry
        if self.closing or entry is None:
            return
        if creds == entry_credentials(entry.data):
            return
        self.persisted_creds = creds
        era = {**entry.data.get(CONF_ERA, {}), "refresh_token": creds["refresh_token"]}
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_ERA: era, CONF_SESSION: creds["session"]})
        _LOGGER.debug("ERA: persisted re-minted credentials to the config entry")

    @callback
    def async_handle_auth_failed(self, message: str) -> None:
        """The ERA account rejected the stored credentials (refresh token
        expired/revoked): make the lock unavailable and start HA's reauth flow
        ONCE. The listener has already stopped its reconnect loop."""
        if self.auth_error is not None or self.closing:
            return
        self.auth_error = message
        if self.controller is not None:
            self.controller.set_auth_failed(message)
        if self.started and self.config_entry is not None:
            # During setup, async_setup_entry raises ConfigEntryAuthFailed
            # instead and HA starts the flow itself.
            self.config_entry.async_start_reauth(self.hass)

    # -- LAN poll -------------------------------------------------------------
    async def _async_update_data(self) -> dict:
        if not self.has_local:
            return {}
        try:
            data = await self.hass.async_add_executor_job(self._poll)
        except SecretsError as e:
            raise UpdateFailed(str(e)) from e
        if data.get("battery") is not None:
            self.async_set_battery(data["battery"], "lan")
        return data

    def _poll(self) -> dict:
        # Tuya hubs intermittently reject the first local connect (error 914);
        # retry a couple of times, forcing a fresh connection each attempt.
        last: Exception | None = None
        for attempt in range(3):
            try:
                dps = self._local.dps()
                break
            except SecretsError as e:
                last = e
                self._local._dev = None  # force reconnect
                if attempt < 2:
                    time.sleep(0.6)
        else:
            raise last  # type: ignore[misc]

        battery = dps.get("8")
        try:
            battery = int(battery)
        except (TypeError, ValueError):
            battery = None
        return {"battery": battery, "dps": dps, "anti_lock": dps.get("11")}
