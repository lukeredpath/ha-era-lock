"""eralock — control library for an ERA Protect Smart Universal Door Handle.

The lock is a Tuya BLE device bridged by a Tuya Wi-Fi hub. This package provides:
  - local:   LAN control/read over the hub (tinytuya, protocol 3.4)  [working]
  - signing: offline Tuya mobile-API request signing                 [working]
  - cloud:   Tuya mobile-API client for cloud-assisted unlock         [in progress]
  - dpmap:   authoritative datapoint map + attribution helpers
  - secrets: loads all key material as configuration values

All secret/key material is loaded at runtime from gitignored JSON (see
secrets.py); nothing sensitive is embedded in source. These load points are the
seam the Home Assistant integration will fill from its config entry.
"""

__version__ = "0.1.0"
