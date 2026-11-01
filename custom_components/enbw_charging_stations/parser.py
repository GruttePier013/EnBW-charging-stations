"""Parsing of EnBW responses (no Home Assistant imports)."""

from __future__ import annotations

import re
from typing import Any

# Internal charge point statuses are English; display text is translated elsewhere.
STATUS_MAP = {
    "AVAILABLE": "available",
    "UNKNOWN": "unknown",
    # Everything that means 'defective / not usable' counts as out of service
    "OUT_OF_SERVICE": "out_of_service",
    "OUT_OF_ORDER": "out_of_service",
    "OUTOFORDER": "out_of_service",
    "INOPERATIVE": "out_of_service",
    "INOPERABLE": "out_of_service",
    "FAULTED": "out_of_service",
    "FAULT": "out_of_service",
    "BROKEN": "out_of_service",
    "BLOCKED": "out_of_service",
    "UNAVAILABLE": "out_of_service",
    "MAINTENANCE": "out_of_service",
    "REMOVED": "out_of_service",
}


def normalize_plug(name: Any) -> str:
    """EnBW returns 'Typ 2' (German); we show 'Type 2'."""
    return re.sub(r"^Typ(?=\s|\d|$)", "Type", str(name or "").strip())


def map_status(raw: Any) -> str:
    """Anything that is not available/out of service/unknown counts as occupied (like the EnBW map)."""
    key = re.sub(r"[\s\-]+", "_", str(raw or "UNKNOWN").strip().upper())
    return STATUS_MAP.get(key, "occupied")


_POSTCODE = re.compile(r"\b\d{4}\s?[A-Za-z]{2}\b|\b\d{5}\b(?=\s+[A-ZÄÖÜ])")


def street_and_number(address: str | None) -> str | None:
    """'Voorbeeldlaan 14, Voorbeeldstad, NL' -> 'Voorbeeldlaan 14' (first part with a house number)."""
    parts = [x.strip() for x in str(address or "").split(",") if x.strip()]
    if not parts:
        return None
    return next((x for x in parts if re.search(r"\d", x)), parts[0])


def clean_address(value: str) -> str:
    """'Voorbeeldlaan 14, 1234AB Voorbeeldstad, NL' -> 'Voorbeeldlaan 14, Voorbeeldstad, NL' (postcode removed)."""
    text = _POSTCODE.sub("", value)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",(\s*,)+", ",", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,")


def list_label(address: str) -> str:
    """'Main Street 1, Exampletown, NL' -> 'Main Street 1, Exampletown' (no country code in the result list)."""
    return re.sub(r",\s*[A-Z]{2}$", "", address.strip())


def format_distance(meters: float) -> str:
    """'140m' below a kilometre, otherwise '1,2km'."""
    if meters < 1000:
        return f"{round(meters)}m"
    return f"{meters / 1000:.1f}km".replace(".", ",")


def station_is_open(detail: dict[str, Any]) -> bool | None:
    times = detail.get("openingTimes") or {}
    if times.get("twentyFourSeven"):
        return True
    if "nextClosingTime" in times:
        return True
    if "nextOpeningTime" in times:
        return False
    return None


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _qualifies(point: dict[str, Any], plugs: list[str], min_power: float) -> bool:
    connectors = [c for c in point.get("connectors") or [] if isinstance(c, dict)]
    if not connectors:
        return not plugs and not min_power
    for c in connectors:
        if plugs and normalize_plug(c.get("plugTypeName")) not in plugs:
            continue
        if (_num(c.get("maxPowerInKw")) or 0) < min_power:
            continue
        return True
    return False


def point_label(number: int, power_kw: float | None) -> str:
    """Label of a charge point, e.g. '1 - 17kW' (the entity name is 'Charge point <label>', translated)."""
    return f"{number} - {power_kw:g}kW" if power_kw else str(number)


def point_name(number: int, power_kw: float | None) -> str:
    """Charge point name in event data and attributes, e.g. 'Charge point 1 - 17kW'."""
    return f"Charge point {point_label(number, power_kw)}"


def build_view(detail: dict[str, Any], plugs: list[str] | None = None, min_power: float = 0) -> dict[str, Any]:
    """Compact view of a station detail, with an optional connector/power filter."""
    plugs, min_power = [normalize_plug(p) for p in plugs or []], float(min_power or 0)
    raw_points = [p for p in detail.get("chargePoints") or [] if isinstance(p, dict)]
    all_points: list[dict[str, Any]] = []
    for number, p in enumerate(raw_points, start=1):
        conns = [c for c in p.get("connectors") or [] if isinstance(c, dict)]
        power = max((_num(c.get("maxPowerInKw")) or 0 for c in conns), default=None)
        all_points.append(
            {
                "number": number,
                "name": point_name(number, power),
                "evse": p.get("evseId"),
                "status": map_status(p.get("status")),
                "status_raw": p.get("status"),
                "connectors": [normalize_plug(c.get("plugTypeName")) for c in conns],
                "power_kw": power,
                "cable_attached": any(c.get("cableAttached") for c in conns),
                "qualifies": _qualifies(p, plugs, min_power),
            }
        )
    points = [p for p in all_points if p["qualifies"]]
    if raw_points:
        total = len(points)
        free = sum(1 for p in points if p["status"] == "available")
        occupied = sum(1 for p in points if p["status"] == "occupied")
        broken = sum(1 for p in points if p["status"] == "out_of_service")
        unknown = sum(1 for p in points if p["status"] == "unknown")
    else:  # no charge point list: fall back to the counters of the station
        total = int(_num(detail.get("numberOfChargePoints")) or 0)
        free = int(_num(detail.get("availableChargePoints")) or 0)
        unknown = int(_num(detail.get("unknownStateChargePoints")) or 0)
        broken = 0
        occupied = max(total - free - unknown, 0)

    names = detail.get("plugTypeNames")
    if not isinstance(names, list):
        names = [c.get("plugTypeName") for p in raw_points for c in p.get("connectors") or [] if isinstance(c, dict)]
    names = sorted({normalize_plug(n) for n in names if n})
    powers = [pt["power_kw"] for pt in points if pt["power_kw"] is not None]
    max_power = max(powers) if powers else _num(detail.get("maxPowerInKw"))
    times = detail.get("openingTimes") or {}
    return {
        "id": str(detail.get("stationId")) if detail.get("stationId") is not None else None,
        "address": detail.get("shortAddress"),
        "lat": _num(detail.get("lat")),
        "lon": _num(detail.get("lon")),
        "total": total,
        "free": free,
        "occupied": occupied,
        "broken": broken,
        "unknown": unknown,
        "open": station_is_open(detail),
        "plug_types": names,
        "max_power_kw": max_power,
        "points": points,
        "all_points": all_points,
        "opening_hours": (
            "24/7" if times.get("twentyFourSeven") else compress_opening_hours(detail.get("openingHours"))
        ),
        "closed_on_holidays": times.get("closedOnHolidays"),
        "next_opening": times.get("nextOpeningTime"),
        "next_closing": times.get("nextClosingTime"),
        "filtered": bool(plugs or min_power),
    }


def summarize(view: dict[str, Any] | None) -> str:
    """State text of the main sensor ('2/4 available', 'Closed', 'No charge points')."""
    if not view:
        return "Unknown"
    if view["open"] is False:
        return "Closed"
    if not view["total"]:
        return "No charge points"
    return f"{view['free']}/{view['total']} available"


_POINT_ICON = {"available": "🅿️", "occupied": "⛔"}


def visual_icons(view: dict[str, Any] | None) -> list[str]:
    """One icon per charge point: 🅿️ available, ⛔ occupied (or station closed), ⚫ something wrong."""
    if not view or not view["total"]:
        return ["⚫"]
    if view["open"] is False:
        return ["⛔"] * view["total"]
    if view["points"]:
        return [_POINT_ICON.get(p["status"], "⚫") for p in view["points"]]
    other = view["total"] - view["free"] - view["occupied"]
    return ["🅿️"] * view["free"] + ["⛔"] * view["occupied"] + ["⚫"] * other


def visual_state(view: dict[str, Any] | None) -> str:
    """E.g. '🅿️/⛔' for two charge points of which one is available."""
    return "/".join(visual_icons(view))


def picture_kind(view: dict[str, Any] | None, stale: bool = False) -> str:
    """'available_2' (2+ free), 'available' (1 free), 'unavailable' (none free/closed) or 'unknown' (no/old data)."""
    if stale or not view:
        return "unknown"
    if not view or not view["total"] or view["open"] is False or view["free"] <= 0:
        return "unavailable"
    return "available_2" if view["free"] >= 2 else "available"


# EnBW sends German day abbreviations; we show English ones.
_DAYS_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_DAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY_PATTERN = re.compile(r"(?<![A-Za-z])(Mo|Di|Mi|Do|Fr|Sa|So):\s*")


def compress_opening_hours(value: str | None) -> str | None:
    """Merge consecutive days with equal hours and translate them: 'Mon-Fri: 08:00 - 20:00'."""
    if not value:
        return value
    parts = _DAY_PATTERN.split(value)
    if parts[0].strip() or len(parts) < 3:
        return value
    entries = [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts), 2)]
    if any(not hours for _, hours in entries):
        return value
    groups: list[list[str]] = []
    for day, hours in entries:
        if groups and groups[-1][2] == hours and _DAYS_DE.index(day) == _DAYS_DE.index(groups[-1][1]) + 1:
            groups[-1][1] = day
        else:
            groups.append([day, day, hours])
    labels = []
    for first, last, hours in groups:
        a, b = _DAYS_EN[_DAYS_DE.index(first)], _DAYS_EN[_DAYS_DE.index(last)]
        labels.append((a if first == last else f"{a}-{b}", hours))
    return ", ".join(f"{label}: {hours}" for label, hours in labels)


def parse_ids(text: str | None) -> list[str]:
    """'12345, 67890' -> ['12345', '67890'] (duplicate ids are dropped)."""
    out: list[str] = []
    for token in re.split(r"[\s,;]+", text or ""):
        token = token.strip()
        if token and re.fullmatch(r"[\w\-.]+", token) and token not in out:
            out.append(token)
    return out
