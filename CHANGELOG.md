# Changelog

## 0.7.2
### Changed
- Zone: only walking, running and cycling (`detected_activity` `walking`, `running`, `on_bicycle`) are ignored now, everything else counts (`in_vehicle`, `still`, `unknown`). Before, only `in_vehicle` counted, so a parked car that reports `still` dropped out of the zone, which ended the visit and the refreshes while still on site.
- Address search: small typos in a street or place name are corrected with the PDOK spellchecker (`Voorbeeldlan` finds `Voorbeeldlaan`). It is used when nothing is found or the best result does not look like what was typed.
- Brand images: the plain car-and-charger picture is the integration icon (`brand/icon.png`) and the picture with the *EnBW* text is the logo (`brand/logo.png`), as shown when you open the integration in Home Assistant.
- Debug logging of the zone logic (visit start, why a refresh was skipped) and the visit state in the diagnostics download.

## 0.7.1
### Fixed
- The 15-minute check for stale data wrote entity states from a worker thread, which Home Assistant logs as an error (`async_write_ha_state ... from a thread other than the event loop`, thousands of times). It now runs in the event loop.
- Whether a refresh was automatic or manual is now fixed when it starts. Two refreshes that overlapped (for example the button during a zone refresh) could mix this up, so the event `charge_point_available` fired for an automatic refresh or was skipped for a manual one.
- A change of the phone's *Detected activity* (for example to `in_vehicle`) now also updates the zone, so the person counts as soon as the activity changes and not only at the next GPS update.

## 0.7.0
### Added
- Companion app support: when a tracked person's phone has the *Detected activity* sensor, only `in_vehicle` counts for the zone (walking, cycling, still and unknown are ignored). Without the sensor nothing changes.
- Event `enbw_charging_stations_person_in_zone` when a person enters the zone of a station (`station_id`, `station_name`, `person`, `entity_id`, `activity`, `distance_m`).
- *Open in HACS* button in the README.
### Changed
- **Breaking:** the integration domain is now `enbw_charging_stations` (folder `custom_components/enbw_charging_stations`), so Home Assistant sees a new integration: remove the old *EnBW Laadpalen* entry and folder, add the integration again, and update automations that use the action `enbw_charging_stations.refresh` or the events `enbw_charging_stations_charge_point_available` and `enbw_charging_stations_person_in_zone`.
- All remaining Dutch in the code is English: opening hours use `Mon-Fri`, default station name is `Station <id>`, device model is `EnBW charging station`. The unused compass helpers are removed.
- **Breaking:** entities are named in English and follow the Home Assistant language (English and Dutch translations included). Entity ids now always start with `enbw_chargepoint_<station>_`, for example `sensor.enbw_chargepoint_voorbeeldlaan_14_distance`.
- Unique ids are renamed, so existing entities keep their history: `points_available`, `availability`, `distance`, `id`, `data_timestamp`, `power_max`, `connector_types`, `points_fetched`, `person_nearby`, `person_distance`, `error_paused`, `zone_radius`, `zone_refresh`, `zone_refresh_approach`, `zone_refresh_interval`, `zone_refresh_stop` and `event`. Existing entity ids are not renamed automatically; use *Settings → Entities → Recreate entity IDs* to get the new ones, and update automations and cards.
- State texts are English: `2/4 available`, `Closed`, `Unknown`, `None`.
- The event `enbw_charging_stations_charge_point_available` fires only at a manual refresh (button or action) and only for charge points that changed from occupied, unknown or out of service to available. Available to available is skipped; automatic refreshes never fire it.
- License is now the Unlicense (public domain) instead of MIT. README has a disclaimer that the integration is unofficial.
### Removed
- The *Open* binary sensor. `open`, `opening_hours`, `next_opening` and `next_closing` remain attributes of the map sensor; the old entity is removed automatically.

## 0.6.3
### Changed
- The duplicate *Laadpunten* sensor is removed: the map sensor (street + house number) has the same state and now carries all its attributes (`charge_points`, `available`, `opening_hours`, …). The old entity is removed automatically; update cards that used it.
- *Afstand tot persoon* is reported in km (state in full precision, shown with 1 decimal) instead of 32000.00 m.
- *Maximaal vermogen* is shown without decimals.
- Dutch names: *Beschikbaarheid (visueel)* and *Vernieuwen bij naderen* (was *Alleen vernieuwen bij naderen*).
### Fixed
- The event *Laadpunt vrijgekomen* also fires when a charge point goes from *unknown* to available, and on every manual refresh (button or action) even when nobody is in the zone. Automatic refreshes still require someone in the zone.

## 0.6.2
### Changed
- Search result list in the config flow: row 1 is the address without country code (`Hoofdlaan 1, Amsterdam`), row 2 is `2/10 beschikbaar, ⚡17kW vermogen, 📍1234567, 140m van huis`. The distance is now measured from your Home Assistant home location; the compass direction is gone.
- README rewritten: shorter intro, clearer setup steps, grouped entity tables and a section showing the four map markers.

## 0.6.1
### Added
- Adjust the map position per station: *Configure → Edit station* shows a map with the pin at the current position. Moving the pin changes the marker on the HA map and the position used for person proximity. *Restore the EnBW position* resets it.

## 0.6.0
### Changed
- **Breaking:** *Laadpaal op kaart* is now a **sensor** instead of a device tracker. Its name is street + house number, its state is `1/9 beschikbaar`, and `latitude`, `longitude` and the marker picture are attributes. The entity id changes (for example `sensor.voorbeeldlaan_14`); the old device tracker is removed automatically. Update dashboards and cards that reference it.
### Added
- Option *Use the availability picture on the HA map* (off = plain icon).
- `LICENSE` (MIT) and `tests/test_repository.py`, which guards the HACS and hassfest requirements.

## 0.5.5
### Changed
- Map markers are now a set of four: `station_available_2` (2 or more free), `station_available` (1 free), `station_unavailable` and `station_unknown`. The *partial* marker is removed.

## 0.5.4
- Version bump only, so HACS offers the fixes of 0.5.3 as an update.

## 0.5.3
### Fixed
- The integration failed to load on recent Home Assistant versions (`cannot import name 'async_extract_referenced_entity_ids'`); the action target selector now uses `homeassistant.helpers.target`. Minimum HA version is now 2026.2.0.
- "Detected blocking call to import_module" warning: the integration is now imported in the executor (`import_executor` in the manifest). The warning was a side effect of the failed import above.
### Changed
- Removed the deprecated `location_name` from the map tracker (unsupported from HA 2027.7).
- Removing stale devices uses `async_remove_device` instead of the deprecated `remove_config_entry_id`.

## 0.5.2
### Changed
- The action is renamed `enbw_laadpalen.refresh_all` → `enbw_laadpalen.refresh`. Without a target it refreshes all stations; with devices, entities or areas as target only those stations.
- *Maximum power*, *Distance to person* and *Connector types* are diagnostic entities; *Connector types* is disabled by default for new installs.
- *Distance to nearest person* is now *Afstand tot persoon* (key `distance_to_person`); the existing sensor is migrated automatically.
- The map entity is named after street + house number.
- The *Refresh all* button no longer has its own device.

## 0.5.1
### Fixed
- Deleting a station via *Delete device* now works and also removes the station from the configuration (it was rejected before).

## 0.5.0
### Added
- New integration logo (README and `brand/` for HA/HACS) and new map markers (available, unavailable, plus a grey question mark for unknown/outdated data).
- Data older than one day: the visual sensor shows ⚫ and the map marker shows the unknown picture.
- Action `enbw_laadpalen.refresh_all` and a *Refresh all* button.
### Changed
- Statuses such as `INOPERATIVE`, `FAULTED`, `BLOCKED` and `UNAVAILABLE` now count as out of service.

## 0.4.3
### Fixed
- The error backoff reused the attribute name `_retry_after`, which `DataUpdateCoordinator` overwrites on init. Renamed to `_backoff_until`.
### Changed
- Startup refreshes at most 5 stations at a time; unloading also clears per-station repair issues; unknown charge points show a question-mark icon; mypy is clean.

## 0.4.2
- Search step: removed the duplicate pair of GPS fields (only the map selector's pair remains) and shortened the intro.

## 0.4.1
- The map entity's status shows `2/4 beschikbaar` instead of the zone name; extra details are attributes.

## 0.4.0
### Added
- **Error protection:** per-station backoff and pause after X errors, and an API-wide circuit breaker (5xx, 429, timeouts; honours `Retry-After`); manual refresh bypasses a pause.
- **Call spacing:** automatic calls are spaced N seconds apart (default 3 s).
- Event `enbw_laadpalen_charge_point_available` plus an event entity.
- Sensor *Distance to nearest person* and the switch *Only refresh when approaching* (on by default).
- Diagnostics download with the raw EnBW response.
- Opening hours with Dutch day names.
### Changed
- **Breaking:** attribute names and internal statuses are English now (`laadpunten` → `charge_points`, `vrij` → `available`, `bezet` → `occupied`, `bijgewerkt` → `updated`, …).
- Recorder load reduced: attributes are no longer stored; statistics only for *Beschikbare laadpunten*.

## 0.3.x
- 0.3.4–0.3.1: result list layout (address row, status and details as helper text), place-name field above the map (PDOK; a bare place name searches towns first), fun fact with total power in the zone, singular/plural headings from the translations.
- 0.3.0: *Defaults* screen after the API key and in *Settings* (with *Apply to existing stations*); the API key moved from the menu to *Settings*.

## 0.2.0
- Per-visit zone refresh (stop-after, cooldown), per-point visual sensor, availability map pictures, checkbox list without numbering, fixed English fallback caused by mismatched translation placeholders.

## 0.1.x
- 0.1.3: *Refresh frequency in zone* and the *Refresh in zone* master switch.
- 0.1.2: EnBW logos and map pictures.
- 0.1.1: renamed to EnBW Laadpalen, coordinate fields under the map, `Type 2` naming, per-charge-point binary sensors, diagnostic *Locatiecode*.

## 0.1.0
- First version: API key, search by map zone, address, coordinates or station id, connector and power filters, refresh schedule per station, proximity, device tracker, NL and EN translations, tests and CI.
