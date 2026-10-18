# Tests

Offline, Home-Assistant-free tests. They need only a stock Python 3.12+ (stdlib)
and this repo: the harness stubs the few `homeassistant` APIs the integration
touches, plus `paho`/`Crypto`/`requests`, and drives the REAL integration code
(controller state engine, listener, event/lock/button entities, logbook,
Cognito client) with a fake clock — no network, no HA install, no secrets.

Run everything from the repo root:

```sh
python3 -I -B tests/offline/run_all.py
```

or a single module, e.g. `python3 -I -B tests/offline/test_regressions.py`.
To test another copy of the package, pass its directory (or set
`ERA_LOCK_PKG`): `python3 -I -B tests/offline/run_all.py path/to/era_lock`.

| Module | Covers |
|---|---|
| `offline/test_controller_harness.py` | The shared harness + the main suite: attribution, door hold, dp61/dp62 coalescing, HA-echo absorption, offline/online, wake coalescing, unlock over the listener's MQTT client |
| `offline/test_regressions.py` | v0.1.2 review probes P1–P7 and the H1/H2/H3/H6 fixes (device-time ordering vs HA inferences, relock failsafe, dp11 bounce, lock service / "Mark as locked") |
| `offline/test_listener.py` | H5: malformed MQTT frames, callback exceptions, the paho-thread watchdog |
| `offline/test_cognito.py` | C1: boto3-free Cognito `InitiateAuth` request shape and error mapping |

All identifiers in these tests are fake.
