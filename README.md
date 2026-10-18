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
> battery sensor, the inferred lock-state engine, and per-credential unlock
> attribution are implemented; the lock-state engine has been validated against
> real hardware.

## What it exposes

- **`lock`** — remote unlock via Tuya cloud MQTT. There is **no remote lock**:
  the lock relocks itself (auto-lock / handle-lift), so `lock.lock` fails with
  an error rather than pretending — automations and voice assistants are never
  told the door locked when it didn't (see **Mark as locked** below). The
  hardware exposes no authoritative lock-state DP, so state is inferred by a
  device-time-ordered engine that is robust to the BLE lock's buffered,
  replayed-on-wake event stream:
  - captures the device's *own* lock signal (`dp11`) by polling for it in the
    seconds after an unlock → **"Device locked"** (covers both the ~30s auto-lock
    and a manual handle-lift). If the polls miss it, a failsafe (≥60s) decides
    what can honestly be concluded: with a door-contact sensor and the door
    kept shut, the lock will have auto-locked → **"Auto-locked (assumed)"**;
    if the door was opened (an entry/exit — the lock then waits for a
    handle-lift) it stays **unlocked** until the lock reports; with no door
    sensor the state becomes **unknown** (**"Lock state not confirmed"**);
  - **survives a restart**: the last known locked/unlocked state is restored
    when Home Assistant starts, shown as inferred (**"Restored after
    restart"**) until the lock reports — any buffered events the startup sync
    flushes (an entry while HA was down, a re-lock) replace it as usual. An
    unknown or offline state isn't restored, and a restored *locked* with the
    door contact reading open comes up **unlocked** (**"Door open after
    restart"**);
  - shows a transient **"Unlocking…"** state during the cloud round-trip;
  - with an optional **door-contact sensor**, opening the door while it is
    locked forces a sync and waits for the lock's buffered events: as soon as
    the fingerprint/card/app unlock that opened it arrives (typically a few
    seconds), the activity log credits it and *then* the lock shows unlocked —
    cause before effect. If nothing turns up within 30s (one extra sync is sent
    half-way) it was the manual **thumbturn**: *Unlocked (thumbturn)*, then
    unlocked. (So after a thumbturn exit the lock reads *locked* for up to 30s
    while the door is open.) After **any** door close while the lock isn't
    known to be locked, it syncs at +10s, +20s and +30s to catch a handle-lift,
    stopping as soon as the lock reports locked (or the door reopens);
  - writes **one rich activity-log line per thing that happened** — *Unlocked by
    Luke (fingerprint)*, *Unlocked (fingerprint #2)*, *Unlocked in Home
    Assistant*, *Auto-locked (assumed)*, *Failed fingerprint attempt*, ... — even
    when a buffered flush nets the state change away; the lock's own state rows
    are linked to (shown as caused by) that line, and it records whether each
    state was **device-confirmed or inferred**.
- **`event`** — **Activity**: a discrete event per unlock/lock/failed
  unlock/tamper/door/connectivity/state-unconfirmed (`event_type` +
  `method`/`credential_id`/`user`/`label`/`description` attributes) for
  automations.
- **`sensor`** — battery %, from the lock's own report each time it wakes
  (cloud MQTT), with a slow (6-hourly) LAN poll as a fallback when a hub IP is
  configured — so it works without a hub IP too.
- **`binary_sensor`** — **Connection**: whether the hub reports the lock
  reachable.
- **`button`** — **Mark as locked**: tell Home Assistant the door is locked
  (e.g. after a lock-up it didn't report). It actuates nothing; the state is
  shown as locked (inferred), credited to you ("Marked as locked in Home
  Assistant"), until the lock itself reports otherwise.
- **`button`** — **Sync**: force the BLE lock to wake and report, flushing any
  buffered events (use sparingly — waking the lock costs battery). Presses a
  few seconds apart are merged into one wake.

### Keeping the lock in sync (battery)

The BLE lock buffers what happens at the door while it sleeps and only reports
when woken, and every wake costs battery. The integration wakes it at moments
that matter: after Home Assistant starts (to flush anything that happened
while it was down), after an unlock (to catch the auto-lock), on door open/close and the
short post-close series above, and on an **idle heartbeat** — by default every
**1 hour with a door-contact sensor** (door events already cover entries and
exits) or every **15 minutes without one**. Leave the *Idle wake interval*
option empty to use that default; a value you've saved explicitly is kept
(installs upgraded from 0.1.2 that saved the settings form have 900s stored —
clear the field to get the new default).

### Sign-in lifecycle

Setup stores an ERA **refresh token**, never your password. The integration
renews its cloud session on its own and saves each renewed session, so restarts
pick up where it left off. If ERA ever rejects the stored sign-in (refresh token
expired or revoked, password changed), the integration stops retrying, the lock
entity becomes **unavailable**, and Home Assistant shows a **re-authenticate**
prompt (Settings → Devices & Services): enter your ERA password and it reconnects.
Network/ERA outages are not treated as sign-in problems — it keeps retrying with
back-off. If the cloud can't be reached at startup, setup is retried by Home
Assistant ("Retrying setup").

### Diagnostics

**Settings → Devices & Services → ERA Protect Lock → ⋮ → Download diagnostics**
gives the connection / lock-state / wake statistics for bug reports, with every
key, token, id, IP address and email redacted.

### Unlock attribution

- **Home Assistant unlocks** are credited to the HA user who issued them.
- **Fingerprints and key cards** are **auto-discovered**: the first time a new
  credential unlocks the door, the integration adds it to the roster and raises a
  notification, so all you do is give it a label and (optionally) map it to a
  Home Assistant user — under **Settings → Devices & Services → ERA Protect Lock →
  Configure → Unlock credentials**.
- When an enrolled credential unlocks the door, the activity log attributes it to
  the mapped **HA user** (naming the method, e.g. *fingerprint*); if it isn't
  mapped, the custom **label** is shown as a fallback. These labels live in Home
  Assistant and are not synced from the ERA cloud.

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
environment, and [`tests/README.md`](tests/README.md) for the offline test
suite (`python3 -I -B tests/offline/run_all.py`, no HA install needed).

## Safety & ethics

This is interoperability code for a lock the maintainer owns. It must only ever
be used against your own device and account. It does not — and must not — ship
the shared Tuya app signing credential.
