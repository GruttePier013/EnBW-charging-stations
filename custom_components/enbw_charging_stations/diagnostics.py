"""Diagnostics download: configuration (API key redacted), health state and the raw EnBW responses."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import CONF_API_KEY, DOMAIN
from .coordinator import EnbwConfigEntry, StationCoordinator

TO_REDACT = {CONF_API_KEY}


def _station_diagnostics(c: StationCoordinator) -> dict[str, Any]:
    return {
        "config": c.station,
        "health": {
            "last_update_success": c.last_update_success,
            "consecutive_errors": c.consecutive_errors,
            "max_errors": c.max_errors,
            "paused": c.paused,
            "data_age_min": c.age_minutes(),
        },
        "settings": {
            "schedule": c.schedule,
            "refresh_on_approach": c.refresh_on_approach,
            "approach_only": c.approach_only,
            "zone_interval_min": c.zone_interval_min,
            "zone_stop_after_min": c.zone_stop_after_min,
            "zone_cooldown_min": c.zone_cooldown_min,
            "proximity_radius": c.proximity_radius,
        },
        "proximity": {
            "nearby": c.nearby,
            "nearest_m": c.nearest_m,
            "nearest_entity": c.nearest_entity,
            "approaching": c.approaching,
            "visit_started": c.visit_started.isoformat() if c.visit_started else None,
            "window_expired": c.window_expired,
            "approach_pending": c.approach_pending,
            "backoff_s": c.backoff_remaining,
        },
        "view": c.view,
        "raw_response": c.data["raw"] if c.data else None,
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EnbwConfigEntry) -> dict[str, Any]:
    data = entry.runtime_data
    return {
        "entry": async_redact_data({"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT),
        "api": {
            "consecutive_errors": data.api.consecutive_errors,
            "outage": data.api.outage,
            "paused_for_s": round(data.api.blocked_for),
            "auth_failed": data.api.auth_failed,
        },
        "stations": {key: _station_diagnostics(c) for key, c in data.coordinators.items()},
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: EnbwConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    coordinators = entry.runtime_data.coordinators
    for domain, key in device.identifiers:
        if domain == DOMAIN and key in coordinators:
            return _station_diagnostics(coordinators[key])
    return {}
