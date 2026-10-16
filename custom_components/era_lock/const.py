"""Constants for the ERA Protect Lock integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "era_lock"

PLATFORMS: list[Platform] = [
    Platform.LOCK, Platform.SENSOR, Platform.EVENT, Platform.BUTTON,
]

# --- config entry data keys --------------------------------------------------
CONF_ERA = "era"                       # refresh token + ERA bridge config
CONF_SESSION = "session"               # eralock.session.Session dict
CONF_SERVICE_CONFIG = "service_config"  # non-secret ERA/Tuya service + device config
CONF_SECRETS = "secrets"               # secret key material (hmac_key, local keys)

# --- options keys ------------------------------------------------------------
CONF_SCAN_INTERVAL = "scan_interval"
CONF_AUTO_RELOCK_SECONDS = "auto_relock_seconds"
CONF_DOOR_CONTACT_ENTITY_ID = "door_contact_entity_id"

DEFAULT_SCAN_INTERVAL = 1800          # 30 min local LAN poll (battery is slow)
DEFAULT_AUTO_RELOCK_SECONDS = 30      # hardware auto-relock window
