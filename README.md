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

> **Status: working, in active development.** Config flow, cloud unlock, the
> battery sensor, and the inferred lock-state engine are functional and have been
> validated against real hardware. Named per-credential attribution (labelling
> fingerprints/key cards and mapping them to HA users) is the current work.

## What it exposes

- **`lock`** — remote unlock via Tuya cloud MQTT. The hardware exposes no
  authoritative lock-state DP and auto-relocks, so state is inferred by a
  device-time-ordered engine that is robust to the BLE lock's buffered,
  replayed-on-wake event stream:
  - captures the device's *own* lock signal (`dp11`) by polling for it in the
    seconds after an unlock → **"Device Locked"** (covers both the ~30s auto-lock
    and a manual handle-lift), with a 60s failsafe timer as a backstop;
  - shows a transient **"Unlocking…"** state during the cloud round-trip;
  - with an optional **door-contact sensor**, infers a manual **thumbturn**
    unlock (the door can't open while deadbolt-locked, so open-while-locked ⇒
    unlocked) and schedules a re-sync ~1 min after an exit to catch a lock-up
    done just after the door shut;
  - records the **cause** of every transition (shown in the activity log) and
    whether each state was **device-confirmed or inferred**.
- **`sensor`** — battery %.
- **`button`** — **Sync**: force the BLE lock to wake and report, flushing any
  buffered events (use sparingly — waking the lock costs battery).

Unlock attribution: Home Assistant unlocks are credited to the HA user who
issued them. Fingerprint/key-card attribution (custom labels, optional HA-user
mapping) is in progress.

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
