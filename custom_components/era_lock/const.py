"""Constants for the ERA Protect Lock integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "era_lock"

# Fired (with a context) as the parent of each lock state change so the activity
# log's "what happened" shows the cause; described in logbook.py.
EVENT_CAUSE = "era_lock_cause"
# Name shown on the cause/annotation logbook rows, kept distinct from the lock
# entity's own name so the two don't read as the same source.
CAUSE_SOURCE = "ERA Protect Lock Activity"

PLATFORMS: list[Platform] = [
    Platform.LOCK, Platform.SENSOR, Platform.BUTTON, Platform.EVENT]

# --- config entry data keys --------------------------------------------------
CONF_ERA = "era"                       # refresh token + ERA bridge config
CONF_SESSION = "session"               # eralock.session.Session dict
CONF_SERVICE_CONFIG = "service_config"  # non-secret ERA/Tuya service + device config
CONF_SECRETS = "secrets"               # secret key material (hmac_key, local keys)

# --- options keys ------------------------------------------------------------
CONF_SCAN_INTERVAL = "scan_interval"
CONF_AUTO_RELOCK_SECONDS = "auto_relock_seconds"
CONF_DOOR_CONTACT_ENTITY_ID = "door_contact_entity_id"
CONF_WAKE_HEARTBEAT = "wake_heartbeat_seconds"
CONF_CREDENTIALS = "credentials"  # [{method, id, label, user_id?, user_name?}]

# Physical credential methods a user can label / map to an HA user. Values match
# dpmap.METHOD_DP_LABELS so a label resolves against inbound unlock events.
CREDENTIAL_METHODS = ("fingerprint", "card", "ble")

DEFAULT_SCAN_INTERVAL = 1800          # 30 min local LAN poll (battery is slow)
DEFAULT_AUTO_RELOCK_SECONDS = 60      # failsafe relock timer (min 60s; device dp11 wins)
DEFAULT_WAKE_HEARTBEAT = 900          # idle wake interval (0 = off); battery vs latency
