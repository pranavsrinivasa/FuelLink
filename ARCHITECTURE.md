# Architecture & Design Decisions

This document explains the architecture of the Fuel-Optimized Route Planner API, the core pipeline, and the reasoning behind all major design decisions (D1-D8).

## 1. System Pipeline

The system is designed to minimize external API calls and deliver route plans in milliseconds.

1. **Input Validation (DRF)**: The user provides start and finish locations.
2. **Geocoding & Routing (OpenRouteService)**: 
   - Geocodes start and finish locations if they aren't already coordinates (cached locally to prevent redundant calls).
   - Fetches the driving polyline (geometry) and total distance using the ORS Directions API.
   - *Optimization*: Uses HTTP Keep-Alive via a shared `requests.Session` to eliminate TLS handshake latency.
3. **Spatial Filtering (NumPy)**: 
   - The route polyline is resampled to ~1-mile intervals.
   - Using a pre-built NumPy array of all station coordinates, the system instantly calculates which stations lie within the route's search corridor (using vectorized operations instead of slow loops).
4. **Fuel Optimizer (Greedy)**: 
   - A pure-Python greedy algorithm evaluates the reachable stations along the route. It drives to the nearest cheaper station if possible; otherwise, it fills up locally to reach a better station ahead.
5. **Response Rendering**: Returns JSON data and a Leaflet-powered `map_url` rendered from a Django template.

---

## 2. Design Decisions (D1 - D8)

### D1: Tiered Offline Geocoding
**Problem**: The provided fuel price CSV has no coordinates. We cannot afford to geocode 6,700 stations dynamically on each request.
**Decision**: A hybrid approach run via `load_stations`. The most popular stations (Top X) are geocoded precisely using ORS. The long tail of stations is assigned city-centroid coordinates using an offline US Census Gazetteer. 
**Why**: Avoids massive rate limits and guarantees fast API responses.

### D2: LRU Route Caching
**Problem**: We have a strict call budget and want maximum performance.
**Decision**: Implementing an in-memory LRU cache (`ROUTE_CACHE_MAX` configurable via `.env`). The cache key is a hash of the start/finish coordinates.
**Why**: A cache hit serves the response in ~2ms with 0 external API calls.

### D3: Vectorized Spatial Filtering (No PostGIS)
**Problem**: We must find stations along the route instantly.
**Decision**: Instead of relying on a heavy PostGIS database (which requires GDAL/GEOS and adds DB latency), all station coordinates and prices are loaded into memory as NumPy arrays at startup. The route polyline is flattened, and distance math (Haversine) is vectorized.
**Why**: Vector math over 6,700 points takes microseconds and eliminates database round-trips.

### D4: Optimal Fuel Stops (Greedy Algorithm with Safety Reserve)
**Problem**: Minimizing total cost while ensuring the vehicle never runs dry.
**Decision**: A greedy algorithm is used. The usable range is strictly `500 - RESERVE_MILES`. The vehicle only proceeds if a station is within reach. If a cheaper station is reachable, it buys just enough fuel to get there; otherwise, it fills up.
**Why**: Fast $O(N \log N)$ execution. A safety reserve ensures model approximations (e.g., city-level coordinates) don't strand the driver in reality.

### D5: API Design (Django + DRF)
**Decision**: The endpoint is built with Django REST Framework (DRF) taking `POST` (or `GET`) requests to `/api/route/`.
**Why**: DRF provides input validation, clean serialization, and a browsable API interface. The core logic is decoupled into the `stations/services/` layer to allow easy unit testing.

### D6: Returning "a map"
**Decision**: Instead of generating a static image (which usually requires a paid API) or just returning GeoJSON, the API returns a URL (`/api/map/<route_id>/`). This URL renders a standalone HTML page using Leaflet and OpenStreetMap tiles.
**Why**: It's free, interactive, requires no API keys, and looks great for demos.

### D7: Performance Strategy
**Decision**: A combination of pre-geocoding, in-memory NumPy indexing, global HTTP Keep-Alive connection pooling, and the LRU route cache.
**Why**: Cold runs process entirely in ~300ms, and warm runs resolve in ~2ms.

### D8: Data Model
**Decision**: SQLite is used purely as the persistent storage (`models.py`) to hold the deduplicated and geocoded stations after the CSV is loaded. On the first API request, the SQLite data is sucked into memory to build the NumPy index (`index.py`).
**Why**: SQLite is zero-configuration and perfectly adequate for storing read-mostly data.
