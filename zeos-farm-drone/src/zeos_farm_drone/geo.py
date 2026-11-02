"""The farm on the ground: where each place is in metres and degrees, for a real flight.

The map (``farm.py``) places the farm in map units. A simulated flight controller flies
in metres and degrees, so here one map unit is ``UNIT_M`` metres, the shed is the
origin, and the origin sits in open grazing country on the Riverine plain near Hay,
New South Wales. PX4 SITL is started with the same origin (``PX4_HOME_LAT``,
``PX4_HOME_LON``, ``PX4_HOME_ALT``), so the drone's home is the shed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from zeos_farm_drone.farm import ON_FARM, PLACES

__all__ = [
    "ORIGIN",
    "UNIT_M",
    "Fix",
    "at",
    "at_metres",
    "bearing_deg",
    "metres_to",
    "metres_between",
    "nearest",
    "offset_m",
]

#: Metres per map unit: paddocks 150 m apart, so a flight between neighbours takes
#: about half a minute at a quadrotor's cruise.
UNIT_M = 150.0


@dataclass(frozen=True, slots=True)
class Fix:
    """A point on the earth: degrees, and metres above mean sea level."""

    lat: float
    lon: float
    alt_m: float


#: The shed, which is the drone's home.
ORIGIN = Fix(lat=-34.5000, lon=144.8500, alt_m=90.0)
_SHED = PLACES["shed"]
_M_PER_DEG_LAT = 111_320.0


def _m_per_deg_lon(lat: float) -> float:
    return _M_PER_DEG_LAT * math.cos(math.radians(lat))


def offset_m(place: str) -> tuple[float, float]:
    """``place`` as (north, east) metres from the shed. The map's y grows southward."""
    x, y = PLACES[place]
    return (-(y - _SHED[1]) * UNIT_M, (x - _SHED[0]) * UNIT_M)


def at(place: str, above_m: float) -> Fix:
    """Where a drone ``above_m`` metres over ``place`` is."""
    north, east = offset_m(place)
    return at_metres(north, east, above_m)


def at_metres(north: float, east: float, above_m: float) -> Fix:
    """The point ``north`` and ``east`` metres from the shed, ``above_m`` over the ground."""
    return Fix(
        lat=ORIGIN.lat + north / _M_PER_DEG_LAT,
        lon=ORIGIN.lon + east / _m_per_deg_lon(ORIGIN.lat),
        alt_m=ORIGIN.alt_m + above_m,
    )


def metres_between(lat: float, lon: float, place: str) -> float:
    """Horizontal metres from a point to a place."""
    north, east = offset_m(place)
    here_north = (lat - ORIGIN.lat) * _M_PER_DEG_LAT
    here_east = (lon - ORIGIN.lon) * _m_per_deg_lon(ORIGIN.lat)
    return math.hypot(here_north - north, here_east - east)


def _towards(lat: float, lon: float, to: Fix) -> tuple[float, float]:
    return (to.lat - lat) * _M_PER_DEG_LAT, (to.lon - lon) * _m_per_deg_lon(ORIGIN.lat)


def bearing_deg(lat: float, lon: float, to: Fix) -> float:
    """The compass bearing from a point to ``to``: 0 is north, 90 east."""
    north, east = _towards(lat, lon, to)
    return math.degrees(math.atan2(east, north)) % 360


def metres_to(lat: float, lon: float, to: Fix) -> float:
    """Horizontal metres from a point to ``to``."""
    return math.hypot(*_towards(lat, lon, to))


def nearest(lat: float, lon: float) -> str:
    """The place on the property nearest a point, which is where a drone stopped
    mid-flight is said to be."""
    return min(sorted(ON_FARM), key=lambda p: metres_between(lat, lon, p))
