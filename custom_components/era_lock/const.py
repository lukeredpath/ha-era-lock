"""Constants for the ERA Protect Lock integration."""
from __future__ import annotations

DOMAIN = "era_lock"

# Platforms are added as later milestones land (lock, sensor, event).
PLATFORMS: list[str] = []

# --- config entry data keys (shapes mirror the eralock library dicts) --------
CONF_ERA = "era"                    # refresh token + ERA bridge config
CONF_SESSION = "session"            # eralock.session.Session dict
CONF_SIGN_SECRETS = "sign_secrets"  # fleet Tuya app signing blob
CONF_LOCK_SECRETS = "lock_secrets"  # per-device: hub_id/cid/local_key/hub_ip/roster

# --- options keys ------------------------------------------------------------
CONF_SCAN_INTERVAL = "scan_interval"
CONF_AUTO_RELOCK_SECONDS = "auto_relock_seconds"
CONF_DOOR_CONTACT_ENTITY_ID = "door_contact_entity_id"

DEFAULT_SCAN_INTERVAL = 1800          # 30 min local LAN poll (battery is slow)
DEFAULT_AUTO_RELOCK_SECONDS = 30      # hardware auto-relock window
