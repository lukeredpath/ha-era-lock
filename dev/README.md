# Local Home Assistant dev environment

A lightweight HA Core instance in a venv for developing the integration. Your
Mac is on the hub LAN and can reach the Tuya cloud, so both the local and cloud
planes work here.

## Setup

Home Assistant supports Python 3.12/3.13 (not 3.14 yet), so use `python3.13`:

```sh
cd dev
python3.13 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install homeassistant

# minimal config dir + live symlink to the component
mkdir -p dev-config/custom_components
ln -s "$(cd .. && pwd)/custom_components/era_lock" dev-config/custom_components/era_lock
```

Create `dev-config/configuration.yaml`:

```yaml
default_config:
logger:
  default: warning
  logs:
    custom_components.era_lock: debug
```

## Run

```sh
cd dev
./.venv/bin/hass -c dev-config
```

Open http://localhost:8123, complete onboarding, then
**Settings → Devices & Services → Add Integration → ERA Protect Lock**.

## Notes

- `dev-config/.storage/` holds the config entry (incl. secrets you enter) and is
  gitignored. Nothing secret is committed.
- **Dependency packaging risk:** the manifest currently lists `boto3` (Cognito)
  and the library uses `requests` (AppSync). Both can collide with Home
  Assistant's own pinned `urllib3`/`requests`. Resolving this — porting Cognito
  to raw HTTPS and AppSync to HA's shared `aiohttp` session — is tracked in the
  plan (Milestone D / credential lifecycle). In this local venv it's harmless.
- `eralock` is vendored at `custom_components/era_lock/eralock/`; this repo is
  its canonical home.
