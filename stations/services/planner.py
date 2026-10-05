import hashlib
import re
import time
from dataclasses import dataclass, field

from django.conf import settings

from .index import get_index
from .lru import LRUCache
from .optimizer import Candidate, InfeasibleRoute, plan_fuel_stops
from .routing import LocationNotFound, OpenRouteServiceClient, RouteNotFound
from .spatial import resample_route, stations_in_corridor

_COORD_RE = re.compile(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$')

_route_cache = None
_geocode_cache = None
_shared_client = None


def _caches():
    global _route_cache, _geocode_cache
    if _route_cache is None:
        _route_cache = LRUCache(settings.ROUTE_CACHE_MAX, settings.ROUTE_CACHE_TTL_SECONDS)
        _geocode_cache = LRUCache(1000, settings.ROUTE_CACHE_TTL_SECONDS)
    return _route_cache, _geocode_cache


def reset_caches():
    global _route_cache, _geocode_cache, _shared_client
    _route_cache = _geocode_cache = _shared_client = None


def get_client():
    global _shared_client
    if _shared_client is None:
        _shared_client = OpenRouteServiceClient()
    return _shared_client


@dataclass
class Location:
    lat: float
    lon: float
    label: str


@dataclass
class RouteContext:
    route_id: str
    start: Location
    finish: Location
    total_miles: float
    geometry: list
    candidates: list
    stations_version: str
    last_payload: dict = field(default=None)


def _in_usa_bounds(lat, lon):
    return 17.0 <= lat <= 72.0 and -180.0 <= lon <= -64.0


def resolve_location(value, client, geocode_cache, stats):
    if isinstance(value, dict):
        lat, lon, label = float(value['lat']), float(value['lon']), None
    elif isinstance(value, str) and _COORD_RE.match(value):
        lat, lon = map(float, _COORD_RE.match(value).groups())
        label = None
    else:
        text = str(value).strip()
        key = text.lower()
        cached = geocode_cache.get(key)
        if cached is None:
            result = client.geocode(text)
            stats['ors_calls'] += 1
            if result is None:
                raise LocationNotFound(f'Could not find "{text}" in the USA.')
            cached = Location(result.lat, result.lon, result.label)
            geocode_cache.set(key, cached)
        return cached
    if not _in_usa_bounds(lat, lon):
        raise LocationNotFound(f'Coordinates ({lat}, {lon}) are outside the USA.')
    return Location(lat, lon, label or f'{lat:.5f}, {lon:.5f}')


def _route_id(start, finish):
    raw = f'{start.lat:.3f},{start.lon:.3f}->{finish.lat:.3f},{finish.lon:.3f}'
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _build_context(route_id, start, finish, client, index, stats):
    raw = client.directions((start.lat, start.lon), (finish.lat, finish.lon))
    stats['ors_calls'] += 1
    route = resample_route(raw.points, raw.distance_miles)
    hits = stations_in_corridor(index.lat, index.lon, index.corridor, route)

    # Stations sharing a mile bucket (e.g. same city centroid): keep the cheapest only.
    best = {}
    for i, mile, off in zip(hits.station_idx, hits.mile, hits.offset):
        bucket = round(float(mile))
        price = float(index.price[i])
        cur = best.get(bucket)
        if cur is None or (price, off) < (cur[0], cur[1]):
            best[bucket] = (price, float(off), int(i), float(mile))
    candidates = [Candidate(index=i, mile=mile, price=price, detour=off)
                  for price, off, i, mile in best.values()]

    geometry = [[round(float(lon), 5), round(float(lat), 5)]
                for lat, lon in zip(route.lats, route.lons)]
    return RouteContext(route_id, start, finish, raw.distance_miles, geometry, candidates,
                        index.version)


def assumptions():
    usable = settings.MAX_RANGE_MILES - settings.RESERVE_MILES
    return {
        'starting_fuel': 'full tank (%d miles of range), not charged to the trip'
                         % settings.MAX_RANGE_MILES
        if settings.START_FUEL_COST_MODE == 'free'
        else 'full tank, charged at the first fuel stop price',
        'max_range_miles': settings.MAX_RANGE_MILES,
        'reserve_miles': settings.RESERVE_MILES,
        'usable_range_miles': usable,
        'mpg': settings.MILES_PER_GALLON,
        'fuel_cost_basis': 'only fuel bought at the listed stops is counted',
        'station_corridor_miles': {
            'address_precision': settings.CORRIDOR_ADDRESS_MILES,
            'city_precision': settings.CORRIDOR_CITY_MILES,
        },
        'station_location_precision': 'address for top stations, city level for the rest',
        'price_source': 'fuel-prices-for-be-assessment.csv (static)',
    }


def _payload(ctx, index, stats, cache_hit, started):
    usable = settings.MAX_RANGE_MILES - settings.RESERVE_MILES
    try:
        plan = plan_fuel_stops(ctx.candidates, ctx.total_miles, usable, settings.MILES_PER_GALLON)
    except InfeasibleRoute as exc:
        raise RouteNotFound(str(exc)) from exc

    stops = []
    for n, s in enumerate(plan.stops, start=1):
        c, m = s.candidate, index.meta[s.candidate.index]
        stops.append({
            'stop': n,
            'station': m['name'], 'address': m['address'], 'city': m['city'], 'state': m['state'],
            'lat': round(float(index.lat[c.index]), 5), 'lon': round(float(index.lon[c.index]), 5),
            'location_precision': m['location_precision'],
            'mile_marker': round(c.mile, 1),
            'miles_off_route': round(c.detour, 1),
            'price_per_gallon': round(c.price, 4),
            'gallons': round(s.gallons, 2),
            'cost_usd': round(s.cost, 2),
            'fuel_range_left_on_arrival_miles': round(s.arrival_fuel_miles + settings.RESERVE_MILES, 1),
        })

    total_cost = plan.total_cost
    if settings.START_FUEL_COST_MODE == 'first_station_price' and plan.stops:
        first = plan.stops[0]
        used = (usable - first.arrival_fuel_miles) / settings.MILES_PER_GALLON
        total_cost += used * first.candidate.price

    return {
        'route_id': ctx.route_id,
        'start': {'label': ctx.start.label, 'lat': ctx.start.lat, 'lon': ctx.start.lon},
        'finish': {'label': ctx.finish.label, 'lat': ctx.finish.lat, 'lon': ctx.finish.lon},
        'total_distance_miles': round(ctx.total_miles, 1),
        'total_fuel_cost_usd': round(total_cost, 2),
        'total_gallons_purchased': round(plan.total_gallons, 2),
        'assumptions': assumptions(),
        'fuel_stops': stops,
        'route_geojson': {'type': 'LineString', 'coordinates': ctx.geometry},
        'meta': {
            'cache_hit': cache_hit,
            'routing_api_calls': stats['ors_calls'],
            'candidate_stations_on_route': len(ctx.candidates),
            'processing_ms': round((time.perf_counter() - started) * 1000, 1),
        },
    }


def plan_route(start_value, finish_value, client=None):
    started = time.perf_counter()
    client = client or get_client()
    route_cache, geocode_cache = _caches()
    index = get_index()
    stats = {'ors_calls': 0}

    start = resolve_location(start_value, client, geocode_cache, stats)
    finish = resolve_location(finish_value, client, geocode_cache, stats)
    route_id = _route_id(start, finish)

    ctx = route_cache.get(route_id)
    cache_hit = ctx is not None and ctx.stations_version == index.version
    if not cache_hit:
        ctx = _build_context(route_id, start, finish, client, index, stats)
    payload = _payload(ctx, index, stats, cache_hit, started)
    ctx.last_payload = payload
    route_cache.set(route_id, ctx)
    return payload


def get_cached_payload(route_id):
    route_cache, _ = _caches()
    ctx = route_cache.get(route_id)
    return ctx.last_payload if ctx else None
