from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_MILES = 3958.8
MILES_PER_DEG_LAT = 69.0
_CHUNK_CELLS = 3_000_000


def cumulative_miles(lats, lons):
    lat, lon = np.radians(lats), np.radians(lons)
    dlat, dlon = np.diff(lat), np.diff(lon)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    seg = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(a))
    return np.concatenate([[0.0], np.cumsum(seg)])


@dataclass
class ResampledRoute:
    lats: np.ndarray
    lons: np.ndarray
    miles: np.ndarray

    @property
    def total_miles(self):
        return float(self.miles[-1])


def resample_route(points, total_distance_miles, step_miles=1.0):
    lats = np.array([p[0] for p in points], dtype=float)
    lons = np.array([p[1] for p in points], dtype=float)
    raw = cumulative_miles(lats, lons)
    if raw[-1] <= 0:
        return ResampledRoute(lats[:1], lons[:1], np.array([0.0]))
    # Scale mile markers so the last one equals the provider's reported distance.
    miles = raw * (total_distance_miles / raw[-1])
    n = max(2, int(np.ceil(total_distance_miles / step_miles)) + 1)
    grid = np.linspace(0.0, total_distance_miles, n)
    return ResampledRoute(np.interp(grid, miles, lats), np.interp(grid, miles, lons), grid)


@dataclass
class CorridorHits:
    station_idx: np.ndarray
    mile: np.ndarray
    offset: np.ndarray


def stations_in_corridor(st_lat, st_lon, st_corridor, route):
    if st_lat.size == 0:
        return CorridorHits(np.array([], dtype=int), np.array([]), np.array([]))
    pad_lat = float(st_corridor.max()) / MILES_PER_DEG_LAT
    mean_lat = float(np.mean(route.lats))
    pad_lon = pad_lat / max(np.cos(np.radians(mean_lat)), 0.2)
    in_box = np.where(
        (st_lat >= route.lats.min() - pad_lat) & (st_lat <= route.lats.max() + pad_lat)
        & (st_lon >= route.lons.min() - pad_lon) & (st_lon <= route.lons.max() + pad_lon))[0]
    if in_box.size == 0:
        return CorridorHits(np.array([], dtype=int), np.array([]), np.array([]))

    chunk = max(1, _CHUNK_CELLS // len(route.lats))
    r_lat, r_lon = route.lats[None, :], route.lons[None, :]
    hit_idx, hit_mile, hit_off = [], [], []
    for start in range(0, in_box.size, chunk):
        ids = in_box[start:start + chunk]
        s_lat, s_lon = st_lat[ids][:, None], st_lon[ids][:, None]
        dy = (s_lat - r_lat) * MILES_PER_DEG_LAT
        dx = (s_lon - r_lon) * MILES_PER_DEG_LAT * np.cos(np.radians(s_lat))
        d = np.hypot(dx, dy)
        nearest = d.argmin(axis=1)
        off = d[np.arange(len(ids)), nearest]
        ok = off <= st_corridor[ids]
        hit_idx.append(ids[ok])
        hit_mile.append(route.miles[nearest[ok]])
        hit_off.append(off[ok])
    return CorridorHits(np.concatenate(hit_idx), np.concatenate(hit_mile), np.concatenate(hit_off))
