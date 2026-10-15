"""Shared entity helpers for the ERA Protect Lock."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def device_info(entry: ConfigEntry) -> DeviceInfo:
    """One device per lock, keyed by the hub id (the entry's unique_id)."""
    return DeviceInfo(
        identifiers={(DOMAIN, str(entry.unique_id))},
        name="ERA Protect Lock",
        manufacturer="ERA",
        model="Smart Universal Door Handle",
    )
