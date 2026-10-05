# Fuel Route Planner API

Django + DRF API: give it a start and finish in the USA and it returns the route, the
cost-optimal fuel stops (500-mile range, 10 mpg) and the total fuel spend. A Leaflet map
page is linked from every response.

## Free services used
| Purpose | Service | Cost |
|---|---|---|
| Routing + geocoding | [OpenRouteService](https://openrouteservice.org) | Free tier, free API key |
| City coordinates | US Census Gazetteer files (`data/`) | Free, offline |
| Map tiles / JS | OpenStreetMap + Leaflet | Free, no key |

## Setup
```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # put your free ORS key in it, and a secret key:
# python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
python manage.py migrate
python manage.py load_stations  # one-time; geocodes the top stations precisely if a key is set
python manage.py runserver
```
`load_stations --skip-geocode` works without a key (city-level locations only).
Restart the server after reloading stations.

## API
`POST /api/route/` (a `GET` with `?start=..&finish=..` also works)
```json
{ "start": "Chicago, IL", "finish": "Los Angeles, CA" }
```
`start`/`finish` may be a place name, `"lat,lon"`, or `{"lat":..,"lon":..}`
(coordinates need only one ORS call).

Response (abridged): `total_distance_miles`, `total_fuel_cost_usd`,
`total_gallons_purchased`, `assumptions`, `fuel_stops[]` (station, price, gallons, cost,
mile marker), `route_geojson`, `map_url`, `meta` (cache hit, API calls, processing ms).

Errors: `400` bad input / location not found, `422` no feasible fuel plan, `502/503`
routing provider problems.

## Architecture Details
For a deep dive into the system pipeline and the specific design decisions (D1-D8), please see the [Architecture Document](ARCHITECTURE.md).

## How it stays fast & Benchmarks
- **Pre-geocoding**: Stations are geocoded once offline and held in memory as NumPy arrays.
- **LRU Cache**: The route context is kept in an LRU cache (`ROUTE_CACHE_MAX`), so repeat requests make zero external calls.
- **HTTP Keep-Alive**: Network calls reuse a shared `requests.Session` to eliminate TLS handshake overhead.
- **Vectorized Spatial Math**: Finding stations along the route uses NumPy arrays instead of a database, taking microseconds.

You can run the benchmark script to test 100 routes against your live server (make sure the server is running first):
```bash
python scripts/benchmark.py --ors-rpm 36 --max-ors-calls 150
```

**Benchmark Results:**
- **Cold pass (new routes)**: ~375 ms average (includes external ORS routing calls)
- **Warm pass (cache hit)**: ~2 ms average (0 external routing calls)
- **HTML Map Render**: ~3 ms average

## Tests
```bash
python manage.py test
```
