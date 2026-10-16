"""Local-poll data coordinator for the ERA Protect Lock.

Polls the hub over the LAN (tinytuya) for battery + the current datapoint
snapshot. The interval is long (battery changes slowly and polling wakes the BLE
link). Live push updates arrive separately via the cloud MQTT listener (later
milestone). If no hub_ip is configured, polling is skipped and the data is empty
(cloud-only setup).
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .eralock.local import LocalLock
from .eralock.secrets import SecretsError
from .session import EraSessionManager

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


class EraLockCoordinator(DataUpdateCoordinator[dict]):
    """Coordinates local-LAN reads and holds the shared runtime objects."""

    def __init__(self, hass: HomeAssistant, entry: "ConfigEntry",
                 mgr: EraSessionManager, combined: dict) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.config_entry = entry
        self.mgr = mgr
        self.combined = combined
        self.listener = None  # EraMqttListener, set during setup
        self.wake = None  # EraWakeManager, set during setup
        self._local = LocalLock(combined)

    @property
    def has_local(self) -> bool:
        return bool(self.combined.get("hub_ip"))

    async def _async_update_data(self) -> dict:
        if not self.has_local:
            return {}
        try:
            return await self.hass.async_add_executor_job(self._poll)
        except SecretsError as e:
            raise UpdateFailed(str(e)) from e

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
