# ERA Protect Lock — Home Assistant integration

A custom [Home Assistant](https://www.home-assistant.io/) integration for the
**ERA Protect Smart Universal Door Handle** — a Tuya BLE lock bridged by a Tuya
Wi-Fi hub and fronted by ERA's cloud. Installable via [HACS](https://hacs.xyz/).

> [!IMPORTANT]
> **Unofficial.** This project is not affiliated with, authorised by, endorsed by,
> or supported by ERA Home Security Ltd or Tuya. It is independent interoperability
> software for a device the author owns. "ERA", "ERA Protect", and the ERA logo are
> trademarks of their respective owner and are used here solely to identify the
> device this integration works with — their use does not imply any endorsement.
> Use at your own risk, on your own device and account. The logo and/or this project
> will be removed on the trademark owner's request.

> **Status: early development.** The repository scaffold, the vendored control
> library (`eralock`), and the integration skeleton are in place. The config
> flow and entities are being built — see the milestones below.

## What it will expose

- **`lock`** — remote unlock via Tuya cloud MQTT; state is best-effort (the
  hardware reports no authoritative lock state and the manual thumbturn is
  silent), improved by an optional door-contact sensor.
- **`sensor`** — battery %.
- **`event`** — unlock events with per-credential attribution (who/how), for the
  Logbook.

## No secrets ship in this repo

This integration contains **no credentials or keys**. The Tuya app signing
material, device keys, and your ERA account tokens are all supplied by you
during setup and stored only in Home Assistant's config entry. Consequently the
integration is **not turnkey for people who don't already have their device's
key material** — that is deliberate.

## Install (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/lukeredpath/ha-era-lock`, category **Integration**.
2. Install **ERA Protect Lock**, restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → ERA Protect Lock.**

## Development

See [`dev/README.md`](dev/README.md) for a local Home Assistant Core dev
environment.

## Safety & ethics

This is interoperability code for a lock the maintainer owns. It must only ever
be used against your own device and account. It does not — and must not — ship
the shared Tuya app signing credential.
