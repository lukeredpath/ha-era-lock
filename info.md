# ERA Protect Lock

Home Assistant integration for the **ERA Protect Smart Universal Door Handle**
(Tuya BLE lock + Wi-Fi hub + ERA cloud).

Exposes (planned): a `lock` entity (cloud unlock + best-effort state), a battery
`sensor`, and an unlock `event` with per-credential attribution.

**Ships no secrets** — all key material is supplied during setup and stored only
in Home Assistant's config entry. Own-device use only.

> Early development — see the repository README for current status.
