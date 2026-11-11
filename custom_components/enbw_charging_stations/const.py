"""Constants for EnBW Laadpalen."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "enbw_charging_stations"
NAME = "EnBW Laadpalen"
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.EVENT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]

CONF_API_KEY = "api_key"
CONF_LAT_OVERRIDE = "lat_override"  # user-adjusted map position of a station (optional)
CONF_LON_OVERRIDE = "lon_override"
CONF_RESET_LOCATION = "reset_location"
CONF_MAP_LOGO = "map_logo"  # show the availability pictures on the HA map (off = plain icon)
CONF_ADDRESS = "address"
CONF_LOCATION = "location"
CONF_STATIONS = "stations"
CONF_SELECTED_STATIONS = "selected_stations"
CONF_STATION_IDS = "station_ids"
CONF_PLUGS = "plug_types"
CONF_MIN_POWER = "min_power"
CONF_PERSONS = "persons"
CONF_DEFAULT_SCHEDULE = "default_schedule"
CONF_DEFAULT_RADIUS = "default_radius"
CONF_DEFAULT_REFRESH_ON_ZONE = "default_refresh_on_approach"
CONF_DEFAULT_ZONE_INTERVAL = "default_zone_interval"
CONF_DEFAULT_ZONE_STOP_AFTER = "default_zone_stop_after"
CONF_DEFAULT_APPROACH_ONLY = "default_approach_only"
CONF_APPLY_EXISTING = "apply_existing"
CONF_SECTION_DEFAULTS = "defaults"
CONF_SECTION_API = "api"
CONF_SECTION_PROTECTION = "protection"
CONF_MAX_ERRORS = "max_errors"  # consecutive errors before a station pauses automatic refreshes
CONF_API_MAX_ERRORS = "api_max_errors"  # consecutive API errors before all automatic refreshes pause
CONF_API_SPACING = "api_spacing"  # seconds between automatic API calls
CONF_ZONE_COOLDOWN = "zone_cooldown"  # minutes the zone must be empty before a new visit starts

DEFAULT_RADIUS = 250
MIN_RADIUS, MAX_RADIUS = 50, 5000
REQUEST_TIMEOUT = 20
PREVIEW_MAX_STATIONS = 25  # maximum number of stations in the config flow list
MIN_QUERY_RADIUS_M = 500  # the API is always queried at least this widely

API_BASE = "https://api.emp.emob-enbw.com/emobility-public-api/api/v1"
API_ORIGIN = "https://www.enbw.com"
STATIC_URL = "/enbw_charging_stations/static"  # map pictures of the station
PICTURE_AVAILABLE = f"{STATIC_URL}/station_available.png"  # 1 charge point available
PICTURE_AVAILABLE_2 = f"{STATIC_URL}/station_available_2.png"  # 2 or more available
PICTURE_UNAVAILABLE = f"{STATIC_URL}/station_unavailable.png"
PICTURE_UNKNOWN = f"{STATIC_URL}/station_unknown.png"  # no or outdated data
SERVICE_REFRESH = "refresh"
PDOK_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
PDOK_SUGGEST_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/suggest"
USER_AGENT = "HomeAssistant-enbw_charging_stations/0.1.0 (custom integration)"

DEFAULT_PROXIMITY_RADIUS = 1000  # meters
DEFAULT_ZONE_COOLDOWN = 15  # minutes
MIN_APPROACH_REFRESH_GAP = 60  # seconds between two entry refreshes

# Error protection defaults
DEFAULT_MAX_ERRORS = 5  # per station
DEFAULT_API_MAX_ERRORS = 10  # across the whole API
DEFAULT_API_SPACING = 3.0  # seconds between automatic API calls (spreads many stations)
STATION_BACKOFF_BASE = 60  # seconds; doubles per consecutive error, capped below
STATION_BACKOFF_MAX = 900
API_BACKOFF_BASE = 60
API_BACKOFF_MAX = 1800
RATE_LIMIT_DEFAULT_WAIT = 300  # seconds to wait after HTTP 429 without Retry-After

# Approach detection
APPROACH_MIN_DELTA_M = 25  # distance must shrink by this much to count as approaching
APPROACH_HOLD_MIN = 5  # minutes a person keeps counting as approaching after the last decrease

EVENT_CHARGE_POINT_AVAILABLE = f"{DOMAIN}_charge_point_available"
EVENT_PERSON_IN_ZONE = f"{DOMAIN}_person_in_zone"
# Companion app activities ("detected_activity") of someone on foot or by bike: they do not count for the zone.
# Everything else (in_vehicle, still, unknown) counts: a parked car often reports "still" on arrival.
ACTIVITIES_IGNORED = frozenset({"walking", "running", "on_bicycle"})

# Entity ids start with this prefix: <domain>.enbw_chargepoint_<station>_<key>
ENTITY_PREFIX = "enbw_chargepoint"
