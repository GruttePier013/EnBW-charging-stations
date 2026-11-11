"""Proximity: automatic "zone enter" per station based on Home Assistant person entities.

No Home Assistant zones are created or changed. The integration compares the GPS position of
persons with the station, starts a refresh visit when someone enters the radius and keeps track
of who is getting closer (used for "approach only" refreshing and the distance sensor).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .api import distance_m
from .const import ACTIVITIES_IGNORED, APPROACH_HOLD_MIN, APPROACH_MIN_DELTA_M
from .coordinator import StationCoordinator

_LOGGER = logging.getLogger(__name__)


def coarse_distance(meters: int) -> int:
    """Round for the distance sensor so a moving person does not cause a state write per GPS fix."""
    if meters < 1000:
        return round(meters / 10) * 10
    if meters < 10_000:
        return round(meters / 100) * 100
    return round(meters / 1000) * 1000


class ProximityTracker:
    def __init__(
        self,
        hass: HomeAssistant,
        coordinators: dict[str, StationCoordinator],
        persons: list[str],
    ) -> None:
        self.hass = hass
        self.coordinators = coordinators
        self.persons = set(persons)  # empty = all persons
        # reference distance per (station, person) used to detect approaching
        self._reference: dict[tuple[str, str], int] = {}

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        for c in self.coordinators.values():
            self.evaluate(c, trigger=False)
        return self.hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            self._handle_state_change,
            event_filter=self._is_relevant_event,
        )

    @callback
    def _is_relevant_event(self, data: EventStateChangedData) -> bool:
        """Person changes, and changes of a phone's activity sensor (for example to in_vehicle)."""
        entity_id = str(data["entity_id"])
        if entity_id.startswith("person."):
            return True
        if not entity_id.startswith("sensor."):
            return False
        entry = er.async_get(self.hass).async_get(entity_id)
        return bool(entry and entry.platform == "mobile_app" and "detected_activity" in (entry.unique_id or ""))

    @callback
    def _handle_state_change(self, event: Event[EventStateChangedData]) -> None:
        for c in self.coordinators.values():
            self.evaluate(c, trigger=True)

    def activity(self, person: str) -> str | None:
        """Companion app activity of a person (``detected_activity`` sensor), or None when there is none.

        The sensor is found through the person's tracker: same device, platform ``mobile_app``.
        None also when the sensor is unavailable, so persons without the sensor keep working as before.
        """
        state = self.hass.states.get(person)
        if state is None:
            return None
        trackers = list(state.attributes.get("device_trackers") or [])
        if source := state.attributes.get("source"):
            trackers.append(source)
        registry = er.async_get(self.hass)
        for tracker in trackers:
            entry = registry.async_get(tracker)
            if entry is None or entry.device_id is None:
                continue
            for candidate in er.async_entries_for_device(registry, entry.device_id):
                if (
                    candidate.platform == "mobile_app"
                    and candidate.domain == "sensor"
                    and "detected_activity" in (candidate.unique_id or "")
                    and (activity := self.hass.states.get(candidate.entity_id))
                    and activity.state not in ("unavailable", "unknown", "")
                ):
                    return activity.state
        return None

    def _counts(self, person: str) -> bool:
        """A person counts for the zone unless the phone reports walking, running or cycling."""
        return self.activity(person) not in ACTIVITIES_IGNORED

    def _positions(self) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for st in self.hass.states.async_all("person"):
            if self.persons and st.entity_id not in self.persons:
                continue
            lat, lon = st.attributes.get("latitude"), st.attributes.get("longitude")
            if lat is None or lon is None:
                continue
            try:
                out[st.entity_id] = (float(lat), float(lon))
            except (TypeError, ValueError):
                continue
        return out

    @callback
    def _update_approach(self, coordinator: StationCoordinator, dists: dict[str, int]) -> None:
        """Mark persons whose distance shrank noticeably as approaching (for a limited time)."""
        now = dt_util.utcnow()
        for entity, distance in dists.items():
            key = (coordinator.key, entity)
            reference = self._reference.get(key)
            if reference is None:
                self._reference[key] = distance
            elif distance <= reference - APPROACH_MIN_DELTA_M:
                coordinator.approach_until[entity] = now + timedelta(minutes=APPROACH_HOLD_MIN)
                coordinator.approach_pending = True
                self._reference[key] = distance
            elif distance >= reference + APPROACH_MIN_DELTA_M:
                self._reference[key] = distance

    @callback
    def evaluate(self, coordinator: StationCoordinator, trigger: bool) -> None:
        loc = coordinator.location
        if loc is None:
            coordinator.set_proximity({}, None)
            return
        dists = {
            entity: round(distance_m(loc[0], loc[1], lat, lon)) for entity, (lat, lon) in self._positions().items()
        }
        self._update_approach(coordinator, dists)
        radius = coordinator.proximity_radius
        nearby = {e: d for e, d in dists.items() if d <= radius and self._counts(e)}
        nearest_entity = min(dists, key=dists.__getitem__) if dists else None
        nearest = coarse_distance(dists[nearest_entity]) if nearest_entity else None
        coordinator.set_proximity(nearby, nearest, trigger=trigger, nearest_entity=nearest_entity)
