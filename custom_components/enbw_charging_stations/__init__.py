"""EnBW Laadpalen: status of EnBW charging stations in Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
from homeassistant.helpers.typing import ConfigType

from .api import EnbwApi
from .const import (
    CONF_API_KEY,
    CONF_API_MAX_ERRORS,
    CONF_API_SPACING,
    CONF_PERSONS,
    DOMAIN,
    NAME,
    PLATFORMS,
    SERVICE_REFRESH,
    STATIC_URL,
)
from .coordinator import EnbwConfigEntry, EnbwData, StationCoordinator, issue_id
from .defaults import get_protection
from .proximity import ProximityTracker
from .stations import CONF_STATIONS, get_stations, station_key

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
API_ISSUE_ID = "api_paused"


# (platform, old key, new key): unique ids are f"{station_key}_{key}"
UNIQUE_ID_RENAMES = (
    ("sensor", "nearest_person_distance", "person_distance"),
    ("sensor", "distance_to_person", "person_distance"),
    ("sensor", "free_points", "points_available"),
    ("sensor", "plug_types", "connector_types"),
    ("sensor", "max_power", "power_max"),
    ("sensor", "nearby_count", "person_nearby"),
    ("sensor", "fetched_points", "points_fetched"),
    ("sensor", "last_update", "data_timestamp"),
    ("sensor", "station_id", "id"),
    ("sensor", "distance_m", "distance"),
    ("binary_sensor", "available", "availability"),
    ("binary_sensor", "paused", "error_paused"),
    ("select", "zone_refresh", "zone_refresh_interval"),
    ("select", "zone_stop_after", "zone_refresh_stop"),
    ("switch", "refresh_on_approach", "zone_refresh"),
    ("switch", "approach_only", "zone_refresh_approach"),
    ("number", "proximity_radius", "zone_radius"),
    ("event", "charge_point_event", "event"),
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    # Pictures of the station on the map (entity_picture of the map marker sensor)
    if http := getattr(hass, "http", None):
        await http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "static"), False)]
        )

    async def refresh(call: ServiceCall) -> None:
        """Action ``enbw_charging_stations.refresh``: no target = all stations, otherwise only the targeted stations."""
        coordinators: dict[str, StationCoordinator] = {}
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coordinators.update(entry.runtime_data.coordinators)
        selected = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
        if selected.referenced or selected.indirectly_referenced or selected.referenced_devices:
            dev_reg = dr.async_get(hass)
            ent_reg = er.async_get(hass)
            device_ids = set(selected.referenced_devices)
            for entity_id in selected.referenced | selected.indirectly_referenced:
                if (reg_entry := ent_reg.async_get(entity_id)) and reg_entry.device_id:
                    device_ids.add(reg_entry.device_id)
            keys = {
                ident[1]
                for device_id in device_ids
                if (device := dev_reg.async_get(device_id))
                for ident in device.identifiers
                if ident[0] == DOMAIN
            }
            coordinators = {k: c for k, c in coordinators.items() if k in keys}
        await asyncio.gather(*(c.async_manual_refresh() for c in coordinators.values()))

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh, schema=vol.Schema(cv.TARGET_SERVICE_FIELDS))
    return True


def _make_outage_handler(hass: HomeAssistant, api: EnbwApi):
    """Create/delete a repair issue when the API pauses automatic refreshes after repeated errors."""

    def handler(outage: bool) -> None:
        if outage:
            ir.async_create_issue(
                hass,
                DOMAIN,
                API_ISSUE_ID,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="api_paused",
                translation_placeholders={"errors": str(api.consecutive_errors)},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, API_ISSUE_ID)

    return handler


async def async_setup_entry(hass: HomeAssistant, entry: EnbwConfigEntry) -> bool:
    if entry.title != NAME:  # renamed to 'EnBW Laadpalen'
        hass.config_entries.async_update_entry(entry, title=NAME)
    protection = get_protection(entry.options)
    api = EnbwApi(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        spacing=protection[CONF_API_SPACING],
        max_errors=protection[CONF_API_MAX_ERRORS],
    )
    ir.async_delete_issue(hass, DOMAIN, API_ISSUE_ID)  # fresh state after a reload
    api.on_outage_change = _make_outage_handler(hass, api)

    coordinators: dict[str, StationCoordinator] = {}
    for station in get_stations(entry):
        coordinator = StationCoordinator(hass, entry, api, station)
        coordinators[coordinator.key] = coordinator

    # First refresh in parallel and without failing: a station that does not answer yet becomes
    # 'unavailable' and recovers at the next attempt.
    # (at most 5 at a time, so a large setup does not start with a burst of requests)
    startup_limit = asyncio.Semaphore(5)

    async def _first_refresh(coordinator: StationCoordinator) -> None:
        async with startup_limit:
            await coordinator.async_manual_refresh()

    await asyncio.gather(*(_first_refresh(c) for c in coordinators.values()))

    entry.runtime_data = EnbwData(api=api, coordinators=coordinators)

    # Removed stations: clean up the device
    dev_reg = dr.async_get(hass)
    current = set(coordinators)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
        if ids and not ids & current:
            dev_reg.async_remove_device(device.id)  # a device belongs to a single config entry

    ent_reg = er.async_get(hass)
    # Renamed entities: keep them (and their history) under the new unique id
    for key in coordinators:
        for domain, old_key, new_key in UNIQUE_ID_RENAMES:
            old = ent_reg.async_get_entity_id(domain, DOMAIN, f"{key}_{old_key}")
            if old and not ent_reg.async_get_entity_id(domain, DOMAIN, f"{key}_{new_key}"):
                ent_reg.async_update_entity(old, new_unique_id=f"{key}_{new_key}")
        # The opening hours are attributes of the map sensor now; the 'open' binary sensor is gone
        if old := ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{key}_open"):
            ent_reg.async_remove(old)

    # The 'Laadpunten' sensor was a duplicate of the map sensor (same state); its attributes moved to the map sensor
    for key in coordinators:
        if old := ent_reg.async_get_entity_id("sensor", DOMAIN, f"{key}_points"):
            ent_reg.async_remove(old)

    # The map marker used to be a device tracker; it is a sensor now
    for key in coordinators:
        if old := ent_reg.async_get_entity_id("device_tracker", DOMAIN, f"{key}_location"):
            ent_reg.async_remove(old)

    # Removed entity (binary_sensor 'Person nearby'): clean up old registry items
    for key in coordinators:
        if old := ent_reg.async_get_entity_id("binary_sensor", DOMAIN, f"{key}_nearby"):
            ent_reg.async_remove(old)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start proximity only after the entities restored their settings (radius, on/off)
    tracker = ProximityTracker(hass, coordinators, list(entry.options.get(CONF_PERSONS, [])))
    entry.runtime_data.tracker = tracker
    entry.async_on_unload(tracker.async_start())

    # Data that becomes older than a day must turn 'unknown' even when nothing triggers an update
    @callback  # without it Home Assistant runs the timer in a worker thread, where state writes are not allowed
    def _check_stale(_now) -> None:
        for coordinator in coordinators.values():
            if coordinator.is_stale:
                coordinator.async_update_listeners()

    entry.async_on_unload(async_track_time_interval(hass, _check_stale, timedelta(minutes=15)))
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnbwConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        ir.async_delete_issue(hass, DOMAIN, API_ISSUE_ID)
        for key in entry.runtime_data.coordinators:  # no stale 'station paused' repair issues
            ir.async_delete_issue(hass, DOMAIN, issue_id(key))
    return unloaded


async def async_remove_config_entry_device(hass: HomeAssistant, entry: EnbwConfigEntry, device: dr.DeviceEntry) -> bool:
    """Deleting a station's device removes that station from the configuration (same as Configure → remove)."""
    ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
    stations = get_stations(entry)
    remaining = [s for s in stations if station_key(s) not in ids]
    if len(remaining) != len(stations):
        # the update listener reloads the entry, which also cleans up the station's entities and issues
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_STATIONS: remaining})
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: EnbwConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
