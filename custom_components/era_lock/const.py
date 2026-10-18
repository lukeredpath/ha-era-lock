"""Constants for the ERA Protect Lock integration."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "era_lock"

# Fired once per discrete activity (unlock/lock/failed_unlock/tamper/door) with
# the activity's cause context; described in logbook.py as the activity log's
# single rich line ("Unlocked by Luke (fingerprint)"). The lock + event entity
# state changes for the same activity share / parent to that context.
EVENT_ACTIVITY = "era_lock_activity"
# LEGACY: the per-state-change cause event fired by earlier versions. No longer
# fired; still described so already-recorded history keeps rendering.
EVENT_CAUSE = "era_lock_cause"
# Logbook source name for the activity rows (shown e.g. as "via <name>" on rows
# whose cause is an activity), kept distinct from the lock entity's own name.
CAUSE_SOURCE = "ERA Protect Lock Activity"

PLATFORMS: list[Platform] = [
    Platform.LOCK, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON,
    Platform.EVENT]

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

# Local LAN poll: only a fallback for the battery % — the lock reports dp8 over
# cloud MQTT whenever it wakes, which the battery sensor uses directly.
DEFAULT_SCAN_INTERVAL = 6 * 3600
DEFAULT_AUTO_RELOCK_SECONDS = 60      # failsafe relock timer (min 60s; device dp11 wins)
# Idle wake interval (0 = off); battery vs latency. Its job is to flush events
# the lock buffered while asleep that nothing else prompted a wake for. With a
# door contact, every entry/exit already forces wakes (open, close, the
# post-close series), so the heartbeat is only a safety net: 1h. Without one,
# a buffered credential entry is only seen by the heartbeat: keep 15 min.
DEFAULT_WAKE_HEARTBEAT = 900
DEFAULT_WAKE_HEARTBEAT_WITH_DOOR = 3600


def default_wake_heartbeat(options) -> int:
    """The idle-wake default for these options (door contact or not)."""
    if options.get(CONF_DOOR_CONTACT_ENTITY_ID):
        return DEFAULT_WAKE_HEARTBEAT_WITH_DOOR
    return DEFAULT_WAKE_HEARTBEAT


def wake_heartbeat(options) -> int:
    """The configured idle-wake interval, else the default for the options."""
    v = options.get(CONF_WAKE_HEARTBEAT)
    if v is None:
        return default_wake_heartbeat(options)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default_wake_heartbeat(options)

# Setup: how long to wait for the cloud MQTT connection (incl. a session
# re-mint if the stored one is stale) before ConfigEntryNotReady.
STARTUP_CONNECT_TIMEOUT_S = 20.0
