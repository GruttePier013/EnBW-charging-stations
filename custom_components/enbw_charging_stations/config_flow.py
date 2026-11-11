"""Config and options flow: API key, defaults, finding stations (map/place/coordinates), station numbers, edit, remove, settings."""

from __future__ import annotations

import logging
from typing import Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AddressNotFound,
    EnbwApi,
    EnbwApiError,
    EnbwAuthError,
    EnbwNotFound,
    bearing_deg,
    distance_m,
)
from .const import (
    CONF_ADDRESS,
    CONF_API_KEY,
    CONF_API_MAX_ERRORS,
    CONF_API_SPACING,
    CONF_APPLY_EXISTING,
    CONF_DEFAULT_APPROACH_ONLY,
    CONF_DEFAULT_RADIUS,
    CONF_DEFAULT_REFRESH_ON_ZONE,
    CONF_DEFAULT_SCHEDULE,
    CONF_DEFAULT_ZONE_INTERVAL,
    CONF_DEFAULT_ZONE_STOP_AFTER,
    CONF_LAT_OVERRIDE,
    CONF_LOCATION,
    CONF_LON_OVERRIDE,
    CONF_MAP_LOGO,
    CONF_MAX_ERRORS,
    CONF_MIN_POWER,
    CONF_PERSONS,
    CONF_PLUGS,
    CONF_RESET_LOCATION,
    CONF_SECTION_API,
    CONF_SECTION_DEFAULTS,
    CONF_SECTION_PROTECTION,
    CONF_STATION_IDS,
    CONF_STATIONS,
    CONF_ZONE_COOLDOWN,
    DEFAULT_RADIUS,
    DEFAULT_ZONE_COOLDOWN,
    DOMAIN,
    MAX_RADIUS,
    MIN_QUERY_RADIUS_M,
    MIN_RADIUS,
    NAME,
    PREVIEW_MAX_STATIONS,
)
from .defaults import get_defaults, get_protection
from .parser import build_view, clean_address, format_distance, list_label, parse_ids, summarize
from .schedules import SCHEDULES, ZONE_INTERVALS, ZONE_STOP_AFTER
from .stations import get_stations, station_key

_LOGGER = logging.getLogger(__name__)

CONF_STATION = "station"
CONF_REMOVE = "remove"


def _search_schema() -> vol.Schema:
    return vol.Schema(
        {
            # 1. Place name or address on top: after submitting, the pin jumps to that place.
            vol.Optional(CONF_ADDRESS): str,
            # 2. Map picker: a pin with a draggable search circle. Home Assistant draws the latitude/
            #    longitude fields (side by side, live synced with the pin) and the radius below the map.
            vol.Required(CONF_LOCATION): selector.LocationSelector(
                selector.LocationSelectorConfig(radius=True, icon="mdi:ev-station")
            ),
        }
    )


def _select(options: list[dict[str, str]], multiple: bool = False) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=cast("list[selector.SelectOptionDict]", options),
            multiple=multiple,
            mode=selector.SelectSelectorMode.LIST if multiple else selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _power_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=400, step=1, unit_of_measurement="kW", mode=selector.NumberSelectorMode.BOX
        )
    )


def _translated_select(options: list[str], key: str) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN, translation_key=key)
    )


def _defaults_fields(values: dict[str, Any]) -> dict[Any, Any]:
    """The single defaults screen (same fields at install time and in Settings)."""
    d = get_defaults(values)
    return {
        vol.Required(CONF_DEFAULT_SCHEDULE, default=d[CONF_DEFAULT_SCHEDULE]): _translated_select(
            list(SCHEDULES), "refresh_schedule"
        ),
        vol.Required(CONF_DEFAULT_RADIUS, default=d[CONF_DEFAULT_RADIUS]): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=MIN_RADIUS, max=MAX_RADIUS, step=50, unit_of_measurement="m", mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(CONF_DEFAULT_REFRESH_ON_ZONE, default=d[CONF_DEFAULT_REFRESH_ON_ZONE]): selector.BooleanSelector(),
        vol.Required(CONF_DEFAULT_APPROACH_ONLY, default=d[CONF_DEFAULT_APPROACH_ONLY]): selector.BooleanSelector(),
        vol.Required(CONF_DEFAULT_ZONE_INTERVAL, default=d[CONF_DEFAULT_ZONE_INTERVAL]): _translated_select(
            list(ZONE_INTERVALS), "zone_refresh"
        ),
        vol.Required(CONF_DEFAULT_ZONE_STOP_AFTER, default=d[CONF_DEFAULT_ZONE_STOP_AFTER]): _translated_select(
            list(ZONE_STOP_AFTER), "zone_stop_after"
        ),
    }


def _protection_fields(values: dict[str, Any]) -> dict[Any, Any]:
    """Error protection: pause a station / the API after repeated errors, spacing between API calls."""
    p = get_protection(values)

    def number(minimum: float, maximum: float, step: float, unit: str | None = None):
        if unit:
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=minimum, max=maximum, step=step, unit_of_measurement=unit, mode=selector.NumberSelectorMode.BOX
                )
            )
        return selector.NumberSelector(
            selector.NumberSelectorConfig(min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX)
        )

    return {
        vol.Required(CONF_MAX_ERRORS, default=p[CONF_MAX_ERRORS]): number(1, 50, 1),
        vol.Required(CONF_API_MAX_ERRORS, default=p[CONF_API_MAX_ERRORS]): number(1, 100, 1),
        vol.Required(CONF_API_SPACING, default=p[CONF_API_SPACING]): number(0, 60, 0.5, "s"),
    }


def _clean_defaults(values: dict[str, Any]) -> dict[str, Any]:
    keys = (
        CONF_DEFAULT_SCHEDULE,
        CONF_DEFAULT_RADIUS,
        CONF_DEFAULT_REFRESH_ON_ZONE,
        CONF_DEFAULT_APPROACH_ONLY,
        CONF_DEFAULT_ZONE_INTERVAL,
        CONF_DEFAULT_ZONE_STOP_AFTER,
    )
    return get_defaults({k: values[k] for k in keys if k in values})


def _api_key_schema(default: str = "") -> vol.Schema:
    return vol.Schema({vol.Required(CONF_API_KEY, default=default): str})


def _station_name(raw: dict[str, Any] | None, sid: str) -> str:
    address = (raw or {}).get("shortAddress")
    return clean_address(str(address)) if address else f"Station {sid}"


class StationFlowMixin:
    """Steps shared by the config flow and the options flow."""

    _candidates: list[dict[str, Any]]
    _location: tuple[float, float]
    _radius: int
    _search_input: dict[str, Any] | None = None
    _queue: list[dict[str, Any]]
    _done: list[dict[str, Any]]

    # --- provided by the subclasses
    def _api_key_value(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _configured_ids(self) -> set[str]:
        return set()

    async def _async_finish_stations(self, stations: list[dict[str, Any]]):  # pragma: no cover - overridden
        raise NotImplementedError

    def _api(self) -> EnbwApi:
        return EnbwApi(async_get_clientsession(self.hass), self._api_key_value())  # type: ignore[attr-defined]

    @staticmethod
    def _error_for(err: BaseException) -> str:
        if isinstance(err, EnbwAuthError):
            return "invalid_auth"
        if isinstance(err, EnbwNotFound):
            return "station_not_found"
        return "cannot_connect"

    # ------------------------------------------------------------ search via map/place/coordinates
    _shown_place: str = ""  # place name the form showed last
    _last_zone: tuple[float, float, int] | None = None

    def _home(self) -> tuple[float, float]:
        return self.hass.config.latitude, self.hass.config.longitude  # type: ignore[attr-defined]

    def _search_form(
        self,
        lat: float,
        lon: float,
        radius: int,
        errors: dict[str, str] | None = None,
        address: str = "",
    ):
        """Show the search form with the pin at the given position."""
        if not errors:
            self._shown_place = address
        suggested = {
            CONF_LOCATION: {"latitude": lat, "longitude": lon, "radius": radius},
            CONF_ADDRESS: address,
        }
        schema = self.add_suggested_values_to_schema(_search_schema(), suggested)  # type: ignore[attr-defined]
        return self.async_show_form(step_id="search", data_schema=schema, errors=errors or {})  # type: ignore[attr-defined]

    async def async_step_search(self, user_input: dict[str, Any] | None = None):
        """Choose the zone.

        The map and its latitude/longitude fields are synced live by Home Assistant itself. A new
        place name is resolved on submit (Enter): the form reopens with the pin at that place;
        submit once more to search.
        """
        home = self._home()
        if user_input is None:
            lat, lon, radius = self._last_zone or (*home, DEFAULT_RADIUS)
            return self._search_form(lat, lon, radius)

        loc = user_input.get(CONF_LOCATION) or {}
        radius = int(min(max(float(loc.get("radius") or DEFAULT_RADIUS), MIN_RADIUS), MAX_RADIUS))
        marker = (
            (float(loc["latitude"]), float(loc["longitude"]))
            if loc.get("latitude") is not None and loc.get("longitude") is not None
            else home
        )
        address = (user_input.get(CONF_ADDRESS) or "").strip()
        if not address:
            self._shown_place = ""

        if address and address != self._shown_place:
            # new place name: move the pin to that place, the user then confirms
            try:
                lat, lon, name = await self._api().async_geocode(address)
            except AddressNotFound:
                return self._search_form(*marker, radius, {"base": "address_not_found"}, address)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Looking up the place name failed")
                return self._search_form(*marker, radius, {"base": "cannot_connect"}, address)
            return self._search_form(lat, lon, radius, address=name)

        self._location = marker
        self._radius = radius
        self._last_zone = (marker[0], marker[1], radius)
        return await self.async_step_load_stations()

    async def async_step_load_stations(self, user_input: dict[str, Any] | None = None):
        """Find stations in the zone and check them live at EnBW."""
        lat, lon = self._location
        api = self._api()
        try:
            raw = await api.async_list(lat, lon, max(self._radius, MIN_QUERY_RADIUS_M))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Search failed", exc_info=True)
            return self.async_abort(reason="invalid_auth" if isinstance(err, EnbwAuthError) else "search_failed")  # type: ignore[attr-defined]
        configured = self._configured_ids()
        scored: list[tuple[float, dict[str, Any]]] = []
        for st in raw:
            if st.get("stationId") is None or st.get("lat") is None or st.get("lon") is None:
                continue
            if str(st["stationId"]) in configured:
                continue
            d = distance_m(lat, lon, float(st["lat"]), float(st["lon"]))
            if d <= self._radius:
                scored.append((d, st))
        scored.sort(key=lambda x: x[0])
        nearest = scored[:PREVIEW_MAX_STATIONS]
        if not nearest:
            return self.async_abort(reason="no_stations")  # type: ignore[attr-defined]
        details = await api.async_details([str(st["stationId"]) for _, st in nearest])
        self._candidates = []
        for d, st in nearest:
            sid = str(st["stationId"])
            detail = details.get(sid)
            live = detail if isinstance(detail, dict) else None
            self._candidates.append(
                {
                    "id": sid,
                    "name": _station_name(live or st, sid),
                    "lat": float(st["lat"]),
                    "lon": float(st["lon"]),
                    "distance_m": round(d),
                    "bearing": bearing_deg(lat, lon, float(st["lat"]), float(st["lon"])),
                    "view": build_view(live) if live else None,
                    "max_power": st.get("maxPowerInKw"),
                }
            )
        return await self.async_step_select_station()

    async def async_step_select_station(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            picked = sorted(int(v) for k, vals in user_input.items() if k.startswith("station_") for v in (vals or []))
            if not picked:
                errors["base"] = "no_selection"
            else:
                # Every chosen station becomes its own device.
                self._queue = [self._pending_from(self._candidates[i]) for i in picked]
                self._done = []
                names = [p["default_name"] for p in self._queue]
                for n, pending in enumerate(self._queue, start=1):
                    if names.count(pending["default_name"]) > 1:
                        pending["default_name"] = f"{pending['default_name']} ({n})"
                return await self.async_step_station_details()
        # One field per station: checkbox + address (row 1) with status and details as helper text (row 2):
        #   ☐ Main Street 1, Exampletown
        #     2/10 available, ⚡17kW power, 📍1234567, 140m from home
        fields: dict[Any, Any] = {}
        placeholders: dict[str, str] = {}
        total_kw = 0.0
        for i, c in enumerate(self._candidates):
            v = c["view"]
            if v:
                free = 0 if v["open"] is False else v["free"]  # closed: nothing available
                status = f"{free}/{v['total']}"
                total_kw += (
                    sum(p["power_kw"] or 0 for p in v["all_points"])
                    if v["all_points"]
                    else (v["max_power_kw"] or 0) * max(v["total"], 1)
                )
            else:
                status = "⚠️ ?/?"
                total_kw += float(c.get("max_power") or 0)
            power = f"{v['max_power_kw']:g}kW" if v and v["max_power_kw"] else "?"
            hass = self.hass  # type: ignore[attr-defined]
            home = distance_m(hass.config.latitude, hass.config.longitude, c["lat"], c["lon"])
            fields[vol.Optional(f"station_{i}", default=[])] = _select(
                [{"label": list_label(c["name"]), "value": str(i)}], multiple=True
            )
            placeholders[f"status_{i}"] = status
            placeholders[f"power_{i}"] = power
            placeholders[f"code_{i}"] = str(c["id"])
            placeholders[f"dist_{i}"] = format_distance(home)
        n = len(self._candidates)
        placeholders |= {"count": str(n), "total": f"{round(total_kw, 1):g}"}
        return self.async_show_form(  # type: ignore[attr-defined]
            # separate step for the singular ('1 charging point') so the translations stay grammatical
            step_id="select_station" if n != 1 else "select_station_single",
            data_schema=vol.Schema(fields),
            description_placeholders=placeholders,
            errors=errors,
        )

    async def async_step_select_station_single(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_select_station(user_input)

    @staticmethod
    def _pending_from(candidate: dict[str, Any]) -> dict[str, Any]:
        v = candidate.get("view") or {}
        return {
            "id": candidate["id"],
            "distance_m": candidate["distance_m"],
            "lat": candidate["lat"],
            "lon": candidate["lon"],
            "default_name": candidate["name"],
            "plug_options": list(v.get("plug_types") or []) if v else None,
            "preview": summarize(candidate["view"]) if candidate.get("view") else "-",
        }

    def _plug_choices(self, available: list[str], current: list[str]) -> list[dict[str, str]]:
        names = sorted({*available, *current})
        return [{"label": n, "value": n} for n in names]

    async def async_step_station_details(self, user_input: dict[str, Any] | None = None):
        pending = self._queue[0]
        if pending.get("plug_options") is None:
            try:
                detail = await self._api().async_detail(pending["id"])
                pending["plug_options"] = build_view(detail)["plug_types"]
            except Exception:  # noqa: BLE001
                pending["plug_options"] = []
        if user_input is not None:
            station = {
                "id": pending["id"],
                "name": (user_input.get(CONF_NAME) or "").strip() or pending["default_name"],
                "plugs": list(user_input.get(CONF_PLUGS) or []),
                "min_power": float(user_input.get(CONF_MIN_POWER) or 0),
            }
            for key in ("distance_m", "lat", "lon"):
                if pending.get(key) is not None:
                    station[key] = pending[key]
            self._done.append(station)
            self._queue.pop(0)
            if self._queue:
                return await self.async_step_station_details()
            return await self._async_finish_stations(self._done)
        fields: dict[Any, Any] = {vol.Required(CONF_NAME, default=pending["default_name"]): str}
        choices = self._plug_choices(pending["plug_options"], [])
        if choices:
            fields[vol.Optional(CONF_PLUGS, default=[])] = _select(choices, multiple=True)
        fields[vol.Optional(CONF_MIN_POWER, default=0)] = _power_selector()
        total = len(self._done) + len(self._queue)
        progress = f"Station {len(self._done) + 1} of {total}" if total > 1 else ""
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="station_details",
            data_schema=vol.Schema(fields),
            description_placeholders={"progress": progress, "status": pending.get("preview") or "-"},
        )

    # ------------------------------------------------------------ manual station numbers
    async def async_step_manual(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            ids = parse_ids(user_input.get(CONF_STATION_IDS))
            if not ids:
                errors["base"] = "invalid_station_id"
            else:
                details = await self._api().async_details(ids)
                bad = next((e for e in details.values() if isinstance(e, BaseException)), None)
                if bad is not None:
                    errors["base"] = self._error_for(bad)
                else:
                    self._queue, self._done = [], []
                    for sid in ids:
                        d = details[sid]
                        v = build_view(d)  # type: ignore[arg-type]
                        pending: dict[str, Any] = {
                            "id": sid,
                            "default_name": _station_name(d, sid),  # type: ignore[arg-type]
                            "plug_options": v["plug_types"],
                            "preview": summarize(v),
                        }
                        if v["lat"] is not None and v["lon"] is not None:
                            pending["lat"], pending["lon"] = v["lat"], v["lon"]
                        self._queue.append(pending)
                    return await self.async_step_station_details()
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_STATION_IDS): str}),
            errors=errors,
        )


class EnbwConfigFlow(StationFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _api_key: str = ""
    _defaults: dict[str, Any] = {}  # noqa: RUF012

    def _api_key_value(self) -> str:
        return self._api_key

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Step 1: API key (once; applies to all stations)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            try:
                await EnbwApi(async_get_clientsession(self.hass), key).async_check_key(
                    self.hass.config.latitude, self.hass.config.longitude
                )
            except EnbwAuthError:
                errors["base"] = "invalid_auth"
            except EnbwApiError:
                errors["base"] = "cannot_connect"
            else:
                self._api_key = key
                return await self.async_step_defaults()
        return self.async_show_form(
            step_id="user", data_schema=_api_key_schema(user_input[CONF_API_KEY] if user_input else ""), errors=errors
        )

    async def async_step_defaults(self, user_input: dict[str, Any] | None = None):
        """Step 2: defaults for refreshing and the zone (one screen; changeable later in Settings)."""
        if user_input is not None:
            self._defaults = _clean_defaults(user_input)
            return await self.async_step_menu()
        return self.async_show_form(step_id="defaults", data_schema=vol.Schema(_defaults_fields(self._defaults)))

    async def async_step_menu(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(step_id="menu", menu_options=["search", "manual"])

    async def _async_finish_stations(self, stations: list[dict[str, Any]]):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=NAME,
            data={CONF_API_KEY: self._api_key, CONF_STATIONS: stations},
            options=get_defaults(self._defaults) if self._defaults else {},
        )

    # ---- re-authenticate when EnBW rejects the key
    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            try:
                await EnbwApi(async_get_clientsession(self.hass), key).async_check_key(
                    self.hass.config.latitude, self.hass.config.longitude
                )
            except EnbwAuthError:
                errors["base"] = "invalid_auth"
            except EnbwApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_API_KEY: key})
        return self.async_show_form(step_id="reauth_confirm", data_schema=_api_key_schema(), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return EnbwOptionsFlow()


class EnbwOptionsFlow(StationFlowMixin, config_entries.OptionsFlow):
    _edit_key: str

    def _api_key_value(self) -> str:
        return self.config_entry.data[CONF_API_KEY]

    def _stations(self) -> list[dict[str, Any]]:
        return get_stations(self.config_entry)

    def _configured_ids(self) -> set[str]:
        return {station_key(s) for s in self._stations()}

    def _save(self, **changes: Any):
        return self.async_create_entry(title="", data={**self.config_entry.options, **changes})

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["search", "manual", "edit_station", "remove_stations", "settings"],
        )

    async def _async_finish_stations(self, stations: list[dict[str, Any]]):
        current = self._stations()
        known = {station_key(s) for s in current}
        new = [s for s in stations if station_key(s) not in known]
        if not new:
            return self.async_abort(reason="already_configured")
        return self._save(**{CONF_STATIONS: [*current, *new]})

    # ------------------------------------------------------------ edit
    async def async_step_edit_station(self, user_input: dict[str, Any] | None = None):
        stations = self._stations()
        if not stations:
            return self.async_abort(reason="no_stations_configured")
        if user_input is not None:
            self._edit_key = user_input[CONF_STATION]
            return await self.async_step_edit_details()
        options = [{"label": f"{s['name']} ({s['id']})", "value": station_key(s)} for s in stations]
        return self.async_show_form(
            step_id="edit_station", data_schema=vol.Schema({vol.Required(CONF_STATION): _select(options)})
        )

    def _original_location(self, station: dict[str, Any]) -> tuple[float, float] | None:
        """The position EnBW reports (configuration or live response), without the user's override."""
        lat, lon = station.get("lat"), station.get("lon")
        if lat is not None and lon is not None:
            return float(lat), float(lon)
        data = getattr(self.config_entry, "runtime_data", None)
        coordinator = data.coordinators.get(station_key(station)) if data else None
        return coordinator.live_location if coordinator else None

    def _effective_location(self, station: dict[str, Any]) -> tuple[float, float] | None:
        if station.get(CONF_LAT_OVERRIDE) is not None and station.get(CONF_LON_OVERRIDE) is not None:
            return float(station[CONF_LAT_OVERRIDE]), float(station[CONF_LON_OVERRIDE])
        return self._original_location(station)

    async def async_step_edit_details(self, user_input: dict[str, Any] | None = None):
        stations = self._stations()
        current = next(s for s in stations if station_key(s) == self._edit_key)
        if user_input is not None:
            updated = {
                **current,
                "name": (user_input.get(CONF_NAME) or "").strip() or current["name"],
                "plugs": list(user_input.get(CONF_PLUGS) or []),
                "min_power": float(user_input.get(CONF_MIN_POWER) or 0),
            }
            # Map position override: moving the pin sets it, 'reset' returns to the EnBW position
            updated.pop(CONF_LAT_OVERRIDE, None)
            updated.pop(CONF_LON_OVERRIDE, None)
            chosen = user_input.get(CONF_LOCATION) or {}
            original = self._original_location(current)
            if not user_input.get(CONF_RESET_LOCATION) and chosen.get("latitude") is not None:
                lat, lon = float(chosen["latitude"]), float(chosen["longitude"])
                if original is None or distance_m(original[0], original[1], lat, lon) >= 1:
                    updated[CONF_LAT_OVERRIDE], updated[CONF_LON_OVERRIDE] = round(lat, 6), round(lon, 6)
            return self._save(**{CONF_STATIONS: [updated if station_key(s) == self._edit_key else s for s in stations]})
        try:
            available = build_view(await self._api().async_detail(current["id"]))["plug_types"]
        except Exception:  # noqa: BLE001
            available = []
        fields: dict[Any, Any] = {vol.Required(CONF_NAME, default=current["name"]): str}
        choices = self._plug_choices(available, current["plugs"])
        if choices:
            fields[vol.Optional(CONF_PLUGS, default=current["plugs"])] = _select(choices, multiple=True)
        fields[vol.Optional(CONF_MIN_POWER, default=current["min_power"])] = _power_selector()
        shown = self._effective_location(current)
        loc_key = (
            vol.Optional(CONF_LOCATION, default={"latitude": shown[0], "longitude": shown[1]})
            if shown
            else (vol.Optional(CONF_LOCATION))
        )
        fields[loc_key] = selector.LocationSelector(
            selector.LocationSelectorConfig(radius=False, icon="mdi:ev-station")
        )
        if CONF_LAT_OVERRIDE in current:
            fields[vol.Optional(CONF_RESET_LOCATION, default=False)] = selector.BooleanSelector()
        return self.async_show_form(step_id="edit_details", data_schema=vol.Schema(fields))

    # ------------------------------------------------------------ remove
    async def async_step_remove_stations(self, user_input: dict[str, Any] | None = None):
        stations = self._stations()
        if user_input is not None:
            remove = set(user_input.get(CONF_REMOVE, []))
            return self._save(**{CONF_STATIONS: [s for s in stations if station_key(s) not in remove]})
        options = [{"label": f"{s['name']} ({s['id']})", "value": station_key(s)} for s in stations]
        return self.async_show_form(
            step_id="remove_stations",
            data_schema=vol.Schema({vol.Optional(CONF_REMOVE, default=[]): _select(options, multiple=True)}),
        )

    # ------------------------------------------------------------ settings
    def _settings_schema(self, values: dict[str, Any], api_key: str) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(CONF_PERSONS, default=values.get(CONF_PERSONS, [])): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="person", multiple=True)
                ),
                vol.Required(
                    CONF_ZONE_COOLDOWN, default=values.get(CONF_ZONE_COOLDOWN, DEFAULT_ZONE_COOLDOWN)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=240, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_MAP_LOGO, default=values.get(CONF_MAP_LOGO, True)): selector.BooleanSelector(),
                vol.Required(CONF_SECTION_DEFAULTS): section(
                    vol.Schema(
                        {
                            **_defaults_fields(values),
                            vol.Required(CONF_APPLY_EXISTING, default=False): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required(CONF_SECTION_PROTECTION): section(
                    vol.Schema(_protection_fields(values)),
                    {"collapsed": True},
                ),
                vol.Required(CONF_SECTION_API): section(
                    vol.Schema({vol.Required(CONF_API_KEY, default=api_key): str}),
                    {"collapsed": True},
                ),
            }
        )

    async def _async_apply_defaults_to_existing(self, d: dict[str, Any]) -> None:
        """Apply the defaults to the existing stations (via the services of their entities)."""
        ent_reg = er.async_get(self.hass)
        on = d[CONF_DEFAULT_REFRESH_ON_ZONE]
        actions: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
            ("select", "refresh_schedule", "select_option", {"option": d[CONF_DEFAULT_SCHEDULE]}),
            ("select", "zone_refresh_interval", "select_option", {"option": d[CONF_DEFAULT_ZONE_INTERVAL]}),
            ("select", "zone_refresh_stop", "select_option", {"option": d[CONF_DEFAULT_ZONE_STOP_AFTER]}),
            ("number", "zone_radius", "set_value", {"value": d[CONF_DEFAULT_RADIUS]}),
            ("switch", "zone_refresh", "turn_on" if on else "turn_off", {}),
            (
                "switch",
                "zone_refresh_approach",
                "turn_on" if d[CONF_DEFAULT_APPROACH_ONLY] else "turn_off",
                {},
            ),
        )
        for station in self._stations():
            for platform, key, service, data in actions:
                entity_id = ent_reg.async_get_entity_id(platform, DOMAIN, f"{station_key(station)}_{key}")
                if not entity_id:
                    continue
                try:
                    await self.hass.services.async_call(
                        platform, service, {"entity_id": entity_id, **data}, blocking=True
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Applying the default to %s failed", entity_id, exc_info=True)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None):
        """Settings: persons, cooldown, defaults (optionally on existing stations), error protection and API key."""
        entry = self.config_entry
        current_key = entry.data[CONF_API_KEY]
        errors: dict[str, str] = {}
        values = {**entry.options}
        api_key = current_key
        if user_input is not None:
            defaults_in = dict(user_input.get(CONF_SECTION_DEFAULTS, {}))
            apply = bool(defaults_in.pop(CONF_APPLY_EXISTING, False))
            cleaned = _clean_defaults(defaults_in)
            api_key = (user_input.get(CONF_SECTION_API, {}).get(CONF_API_KEY) or "").strip() or current_key
            protection = get_protection(dict(user_input.get(CONF_SECTION_PROTECTION, {})))
            values = {
                **values,
                **cleaned,
                **protection,
                CONF_PERSONS: list(user_input.get(CONF_PERSONS, [])),
                CONF_ZONE_COOLDOWN: int(user_input.get(CONF_ZONE_COOLDOWN, DEFAULT_ZONE_COOLDOWN)),
                CONF_MAP_LOGO: bool(user_input.get(CONF_MAP_LOGO, True)),
            }
            if api_key != current_key:
                try:
                    await EnbwApi(async_get_clientsession(self.hass), api_key).async_check_key(
                        self.hass.config.latitude, self.hass.config.longitude
                    )
                except EnbwAuthError:
                    errors["base"] = "invalid_auth"
                except EnbwApiError:
                    errors["base"] = "cannot_connect"
            if not errors:
                if api_key != current_key:
                    self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_API_KEY: api_key})
                if apply:
                    await self._async_apply_defaults_to_existing(cleaned)
                return self._save(**values)
        return self.async_show_form(
            step_id="settings", data_schema=self._settings_schema(values, api_key), errors=errors
        )
