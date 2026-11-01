"""Per-station coordinator: fetching, error protection, zone refresh visits, charge point events."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EnbwApi, EnbwApiError, EnbwAuthError, EnbwBlocked
from .const import (
    CONF_LAT_OVERRIDE,
    CONF_LON_OVERRIDE,
    CONF_MAX_ERRORS,
    CONF_ZONE_COOLDOWN,
    DEFAULT_PROXIMITY_RADIUS,
    DEFAULT_ZONE_COOLDOWN,
    DOMAIN,
    EVENT_CHARGE_POINT_AVAILABLE,
    EVENT_PERSON_IN_ZONE,
    MIN_APPROACH_REFRESH_GAP,
    STATION_BACKOFF_BASE,
    STATION_BACKOFF_MAX,
)
from .defaults import get_protection
from .parser import build_view
from .schedules import (
    DEFAULT_SCHEDULE,
    DEFAULT_ZONE_INTERVAL,
    DEFAULT_ZONE_STOP_AFTER,
    SCHEDULES,
    ZONE_INTERVALS,
    ZONE_STOP_AFTER,
)
from .stations import station_key

_LOGGER = logging.getLogger(__name__)
STALE_AFTER = timedelta(days=1)  # data older than this is shown as unknown


@dataclass
class EnbwData:
    api: EnbwApi
    coordinators: dict[str, StationCoordinator]
    tracker: Any = None  # ProximityTracker


type EnbwConfigEntry = ConfigEntry[EnbwData]


def issue_id(key: str) -> str:
    return f"station_paused_{key}"


class StationCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches the status of one charging station."""

    def __init__(self, hass: HomeAssistant, entry: EnbwConfigEntry, api: EnbwApi, station: dict[str, Any]) -> None:
        self.entry = entry
        self.api = api
        self.station = station
        self.key = station_key(station)
        self.station_id: str = station["id"]
        self.plugs: list[str] = station["plugs"]
        self.min_power: float = station["min_power"]
        self.schedule = DEFAULT_SCHEDULE
        self._unsub_schedule: list[CALLBACK_TYPE] = []
        self._live_location: tuple[float, float] | None = None
        # error protection
        self.max_errors: int = get_protection(entry.options)[CONF_MAX_ERRORS]
        self.consecutive_errors = 0
        self.paused = False
        self._backoff_until = 0.0  # monotonic; automatic refreshes wait until then
        self._auto = False
        # proximity (set by the tracker and the entities)
        self.proximity_radius: int = DEFAULT_PROXIMITY_RADIUS
        self.refresh_on_approach: bool = True
        self.approach_only: bool = True
        self.zone_interval_min: int | None = ZONE_INTERVALS[DEFAULT_ZONE_INTERVAL]  # None = on entry only
        self.zone_stop_after_min: int | None = ZONE_STOP_AFTER[DEFAULT_ZONE_STOP_AFTER]  # None = unlimited
        self.zone_cooldown_min: int = int(entry.options.get(CONF_ZONE_COOLDOWN, DEFAULT_ZONE_COOLDOWN))
        self.nearby: dict[str, int] = {}
        self.nearest_m: int | None = None
        self.nearest_entity: str | None = None
        self.approach_until: dict[str, datetime] = {}
        self.approach_pending = False  # someone got closer since the last interval refresh
        # visit bookkeeping
        self._visit_start: datetime | None = None
        self._window_expired = False
        self._unsub_zone: CALLBACK_TYPE | None = None
        self._unsub_window: CALLBACK_TYPE | None = None
        self._unsub_cooldown: CALLBACK_TYPE | None = None
        self.last_fetch_monotonic: float = 0.0
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{self.key}",
            update_interval=None,  # refreshing is driven by the schedule (select entity), zone and button
        )
        ir.async_delete_issue(hass, DOMAIN, issue_id(self.key))  # fresh state after a reload

    # ---- refresh entry points
    async def async_auto_refresh(self) -> None:
        """Automatic refresh (schedule, zone): honours pause, backoff and the API circuit breaker."""
        if self.paused or self.api.blocked or time.monotonic() < self._backoff_until:
            _LOGGER.debug("Automatic refresh of %s skipped (paused or backing off)", self.key)
            return
        self._auto = True
        await self.async_refresh()

    async def async_manual_refresh(self) -> None:
        """Manual refresh (button, startup): bypasses pause and pacing; success resumes a paused station."""
        self._auto = False
        await self.async_refresh()

    # ---- refresh schedule (set by the select entity)
    def set_schedule(self, key: str) -> None:
        self._cancel_schedule()
        self.schedule = key if key in SCHEDULES else DEFAULT_SCHEDULE
        for hours, minutes in SCHEDULES[self.schedule] or []:
            self._unsub_schedule.append(
                async_track_time_change(self.hass, self._async_scheduled_refresh, hour=hours, minute=minutes, second=0)
            )

    def _cancel_schedule(self) -> None:
        for unsub in self._unsub_schedule:
            unsub()
        self._unsub_schedule = []

    async def _async_scheduled_refresh(self, _now: datetime) -> None:
        await self.async_auto_refresh()

    # ---- refreshing in the zone: one 'visit' with a window
    #  - first person inside: refresh once (real entries only) + every X minutes
    #  - after 'stop after' minutes refreshing stops, even if you are still there
    #  - a new visit only starts once the zone was empty for the cooldown time
    #  - with 'approach only' the interval refreshes only while someone is getting closer
    def _cancel(self, *names: str) -> None:
        for name in names:
            unsub = getattr(self, name)
            if unsub:
                setattr(self, name, None)
                unsub()

    @property
    def approaching(self) -> bool:
        """True while a person inside the zone recently got closer to the station."""
        now = dt_util.utcnow()
        return any(until > now and entity in self.nearby for entity, until in self.approach_until.items())

    @callback
    def _start_window(self) -> None:
        self._cancel("_unsub_window")
        self._window_expired = False
        if self.zone_stop_after_min is None or self._visit_start is None:
            return
        remaining = self.zone_stop_after_min * 60 - (dt_util.utcnow() - self._visit_start).total_seconds()
        if remaining <= 0:
            self._window_expired = True
            return
        self._unsub_window = async_call_later(self.hass, remaining, self._window_end)

    @callback
    def _window_end(self, _now: datetime) -> None:
        self._unsub_window = None
        self._window_expired = True
        self._cancel("_unsub_zone")

    @callback
    def _start_interval(self) -> None:
        self._cancel("_unsub_zone")
        if self._visit_start is None or self._window_expired or not self.zone_interval_min:
            return
        self._unsub_zone = async_track_time_interval(
            self.hass, self._async_zone_tick, timedelta(minutes=self.zone_interval_min)
        )

    async def _async_zone_tick(self, _now: datetime) -> None:
        if not (self.refresh_on_approach and self.nearby and not self._window_expired):
            return
        if self.approach_only:
            if not self.approach_pending:
                return
            self.approach_pending = False
        await self.async_auto_refresh()

    @callback
    def _visit_enter(self, trigger: bool) -> None:
        self._cancel("_unsub_cooldown")
        if self._visit_start is not None:  # back within the cooldown time: same visit, no new start
            return
        self._visit_start = dt_util.utcnow()
        self.approach_pending = False  # the entry refresh covers the approach so far
        if trigger and time.monotonic() - self.last_fetch_monotonic >= MIN_APPROACH_REFRESH_GAP:
            self.hass.async_create_task(self.async_auto_refresh())
        self._start_window()
        self._start_interval()

    @callback
    def _visit_leave(self) -> None:
        self._cancel("_unsub_cooldown")
        if self.zone_cooldown_min <= 0:
            self._visit_end()
            return
        self._unsub_cooldown = async_call_later(self.hass, self.zone_cooldown_min * 60, self._visit_end)

    @callback
    def _visit_end(self, _now: datetime | None = None) -> None:
        self._cancel("_unsub_zone", "_unsub_window", "_unsub_cooldown")
        self._visit_start = None
        self._window_expired = False

    @callback
    def apply_zone_settings(self) -> None:
        """After a change of the switch or selects: set up the timers again."""
        if not self.refresh_on_approach:
            self._visit_end()
            return
        if self._visit_start is None:
            if self.nearby:
                self._visit_enter(trigger=False)
            return
        self._start_window()
        self._start_interval()

    async def async_shutdown(self) -> None:
        self._cancel_schedule()
        self._visit_end()
        await super().async_shutdown()

    # ---- fetching
    async def _async_update_data(self) -> dict[str, Any]:
        self.last_fetch_monotonic = time.monotonic()
        previous = self.view
        auto = self._auto  # a refresh that starts meanwhile must not change what this one is
        try:
            detail = await self.api.async_detail(self.station_id, auto=auto)
        except EnbwAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnbwBlocked as err:  # API paused between the check and the call: not the station's fault
            raise UpdateFailed(str(err)) from err
        except EnbwApiError as err:
            self._register_error()
            raise UpdateFailed(f"Fetching EnBW data failed ({err})") from err
        self._register_success()
        view = build_view(detail, self.plugs, self.min_power)
        if view["lat"] is not None and view["lon"] is not None:
            self._live_location = (view["lat"], view["lon"])
        self._detect_freed_points(previous, view, auto=auto)
        return {
            "view": view,
            "raw": detail,
            "raw_points": len(detail.get("chargePoints") or []),
            "fetched": dt_util.utcnow(),
        }

    # ---- error protection
    def _register_error(self) -> None:
        self.consecutive_errors += 1
        backoff = min(STATION_BACKOFF_BASE * 2 ** (self.consecutive_errors - 1), STATION_BACKOFF_MAX)
        self._backoff_until = time.monotonic() + backoff
        if self.consecutive_errors >= self.max_errors and not self.paused:
            self.paused = True
            _LOGGER.warning(
                "Station %s paused after %d consecutive errors; press Refresh to try again",
                self.key,
                self.consecutive_errors,
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id(self.key),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="station_paused",
                translation_placeholders={"name": self.station["name"], "errors": str(self.consecutive_errors)},
            )
        # the base class only notifies listeners on a state change; the error counters changed too
        self.async_update_listeners()

    def _register_success(self) -> None:
        self.consecutive_errors = 0
        self._backoff_until = 0.0
        if self.paused:
            self.paused = False
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(self.key))
            _LOGGER.info("Station %s is working again", self.key)

    # ---- events
    def _person_names(self) -> list[str]:
        names = []
        for entity_id in sorted(self.nearby, key=lambda e: self.nearby[e]):
            state = self.hass.states.get(entity_id)
            names.append((state.attributes.get("friendly_name") if state else None) or entity_id)
        return names

    def _detect_freed_points(self, old: dict[str, Any] | None, new: dict[str, Any], *, auto: bool) -> None:
        """Fire an event for every charge point that became available at a manual refresh (button/action).

        Only a change *to* available counts (from occupied, unknown or out of service); available to
        available is skipped. Automatic refreshes never fire the event.
        """
        if old is None or auto:
            return
        before = {(p["evse"] or p["number"]): p["status"] for p in old["points"]}
        for point in new["points"]:
            previous = before.get(point["evse"] or point["number"])
            if point["status"] == "available" and previous is not None and previous != "available":
                self.hass.bus.async_fire(
                    EVENT_CHARGE_POINT_AVAILABLE,
                    {
                        "station_id": self.station_id,
                        "station_name": self.station["name"],
                        "charge_point": point["name"],
                        "number": point["number"],
                        "evse": point["evse"],
                        "power_kw": point["power_kw"],
                        "connectors": point["connectors"],
                        "available": new["free"],
                        "total": new["total"],
                        "persons": self._person_names(),
                    },
                )

    # ---- accessors
    @property
    def view(self) -> dict[str, Any] | None:
        return self.data["view"] if self.data else None

    @property
    def live_location(self) -> tuple[float, float] | None:
        """Position from the last EnBW response (None until a response with coordinates arrived)."""
        return self._live_location

    @property
    def location(self) -> tuple[float, float] | None:
        """Position of the station: the user's override, else the configuration, else the live response."""
        lat, lon = self.station.get(CONF_LAT_OVERRIDE), self.station.get(CONF_LON_OVERRIDE)
        if lat is not None and lon is not None:
            return float(lat), float(lon)
        lat, lon = self.station.get("lat"), self.station.get("lon")
        if lat is not None and lon is not None:
            return float(lat), float(lon)
        return self._live_location

    @callback
    def set_proximity(
        self,
        nearby: dict[str, int],
        nearest: int | None,
        trigger: bool = False,
        nearest_entity: str | None = None,
    ) -> None:
        if nearby == self.nearby and nearest == self.nearest_m and nearest_entity == self.nearest_entity:
            return
        was_empty = not self.nearby
        entered = set(nearby) - set(self.nearby)
        self.nearby = nearby
        self.nearest_m = nearest
        self.nearest_entity = nearest_entity
        if trigger:
            for person in sorted(entered):
                self._fire_person_in_zone(person, nearby[person])
        if self.refresh_on_approach:
            if nearby and was_empty:
                self._visit_enter(trigger)
            elif not nearby and not was_empty:
                self._visit_leave()
        self.async_update_listeners()

    def _fire_person_in_zone(self, person: str, distance: int) -> None:
        """Bus event when a person enters the zone (driving, or without companion app activity)."""
        state = self.hass.states.get(person)
        tracker = self.entry.runtime_data.tracker
        self.hass.bus.async_fire(
            EVENT_PERSON_IN_ZONE,
            {
                "station_id": self.station_id,
                "station_name": self.station["name"],
                "person": (state.attributes.get("friendly_name") if state else None) or person,
                "entity_id": person,
                "activity": tracker.activity(person) if tracker else None,
                "distance_m": distance,
            },
        )

    @property
    def is_stale(self) -> bool:
        """True when there is no data or the last successful update is older than a day."""
        if not self.data:
            return True
        return dt_util.utcnow() - self.data["fetched"] > STALE_AFTER

    @property
    def fresh_view(self) -> dict[str, Any] | None:
        """The view when it is current (successful and not older than a day), otherwise None."""
        return self.view if self.last_update_success and self.data and not self.is_stale else None

    def age_minutes(self) -> int | None:
        if not self.data:
            return None
        return round((dt_util.utcnow() - self.data["fetched"]).total_seconds() / 60)
