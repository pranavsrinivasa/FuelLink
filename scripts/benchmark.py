import argparse
import math
import random
import statistics
import sys
import time
from collections import deque

import requests

CITIES = {
    'New York, NY': (40.7128, -74.0060), 'Boston, MA': (42.3601, -71.0589),
    'Philadelphia, PA': (39.9526, -75.1652), 'Washington, DC': (38.9072, -77.0369),
    'Atlanta, GA': (33.7490, -84.3880), 'Miami, FL': (25.7617, -80.1918),
    'Charlotte, NC': (35.2271, -80.8431), 'Nashville, TN': (36.1627, -86.7816),
    'Chicago, IL': (41.8781, -87.6298), 'Detroit, MI': (42.3314, -83.0458),
    'Minneapolis, MN': (44.9778, -93.2650), 'St. Louis, MO': (38.6270, -90.1994),
    'Kansas City, MO': (39.0997, -94.5786), 'Dallas, TX': (32.7767, -96.7970),
    'Houston, TX': (29.7604, -95.3698), 'San Antonio, TX': (29.4241, -98.4936),
    'Oklahoma City, OK': (35.4676, -97.5164), 'Denver, CO': (39.7392, -104.9903),
    'Salt Lake City, UT': (40.7608, -111.8910), 'Phoenix, AZ': (33.4484, -112.0740),
    'Las Vegas, NV': (36.1699, -115.1398), 'Los Angeles, CA': (34.0522, -118.2437),
    'San Francisco, CA': (37.7749, -122.4194), 'Portland, OR': (45.5152, -122.6784),
    'Seattle, WA': (47.6062, -122.3321), 'Boise, ID': (43.6150, -116.2023),
    'Albuquerque, NM': (35.0844, -106.6504), 'New Orleans, LA': (29.9511, -90.0715),
    'Memphis, TN': (35.1495, -90.0490), 'Omaha, NE': (41.2565, -95.9345),
}


def miles(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2)
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def build_routes(count, seed):
    rng = random.Random(seed)
    pairs = [(a, b) for a in CITIES for b in CITIES
             if a != b and 150 <= miles(CITIES[a], CITIES[b]) <= 2800]
    rng.shuffle(pairs)
    if count > len(pairs):
        sys.exit(f'Only {len(pairs)} distinct routes available.')
    return pairs[:count]


def pct(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def summarize(title, values_ms):
    if not values_ms:
        print(f'{title:<34} no data')
        return
    print(f'{title:<34} n={len(values_ms):<4} total={sum(values_ms) / 1000:>7.2f}s  '
          f'avg={statistics.mean(values_ms):>8.1f}  p50={pct(values_ms, 50):>8.1f}  '
          f'p95={pct(values_ms, 95):>8.1f}  max={max(values_ms):>8.1f} ms')


class OrsLimiter:
    def __init__(self, per_minute, max_calls):
        self.per_minute = per_minute
        self.max_calls = max_calls
        self.stamps = deque()
        self.total = 0

    def wait(self):
        while True:
            now = time.monotonic()
            while self.stamps and now - self.stamps[0] >= 60:
                self.stamps.popleft()
            if len(self.stamps) < self.per_minute:
                return
            time.sleep(60 - (now - self.stamps[0]) + 0.2)

    def record(self, calls):
        for _ in range(calls):
            self.stamps.append(time.monotonic())
        self.total += calls

    def over_budget(self):
        return self.total >= self.max_calls


def request_route(session, url, body, limiter, retries=3):
    for attempt in range(retries + 1):
        limiter.wait()
        started = time.perf_counter()
        resp = session.post(url, json=body, timeout=60)
        elapsed = (time.perf_counter() - started) * 1000
        if resp.status_code == 200:
            limiter.record(resp.json()['meta']['routing_api_calls'])
            return resp.json(), elapsed, None
        if resp.status_code in (502, 503) and 'rate limit' in resp.text.lower():
            limiter.record(1)
            if attempt < retries:
                backoff = 30 * (attempt + 1)
                print(f'  provider rate limit hit, backing off {backoff}s')
                time.sleep(backoff)
                continue
        return None, elapsed, f'{resp.status_code} {resp.text[:120]}'
    return None, elapsed, 'retries exhausted'


def main():
    ap = argparse.ArgumentParser(description='Benchmark the live fuel route API.')
    ap.add_argument('--base-url', default='http://127.0.0.1:8000')
    ap.add_argument('--routes', type=int, default=100)
    ap.add_argument('--ors-rpm', type=int, default=36,
                    help='max ORS calls per minute (free tier allows 40 directions/min)')
    ap.add_argument('--max-ors-calls', type=int, default=150,
                    help='abort before spending more of the free daily quota (2000/day)')
    ap.add_argument('--seed', type=int, default=7)
    args = ap.parse_args()

    api = f'{args.base_url.rstrip("/")}/api/route/'
    routes = build_routes(args.routes, args.seed)
    session = requests.Session()
    try:
        session.get(api, timeout=5)
    except requests.RequestException as exc:
        sys.exit(f'Server not reachable at {args.base_url}: {exc}')

    print(f'Benchmarking {len(routes)} distinct routes against {api}\n')
    print(f'Benchmarking {len(routes)} distinct routes against {api}')
    print(f'ORS limits respected: max {args.ors_rpm} calls/min, budget {args.max_ors_calls} calls '
          f'(coordinates are used, so 1 ORS call per new route)\n')
    limiter = OrsLimiter(args.ors_rpm, args.max_ors_calls)
    results, errors = [], []

    cold_start = time.perf_counter()
    for n, (a, b) in enumerate(routes, 1):
        if limiter.over_budget():
            print(f'ORS call budget of {args.max_ors_calls} reached, stopping early.')
            break
        body = {'start': {'lat': CITIES[a][0], 'lon': CITIES[a][1]},
                'finish': {'lat': CITIES[b][0], 'lon': CITIES[b][1]}}
        data, ms, err = request_route(session, api, body, limiter)
        if err:
            errors.append((a, b, err))
            print(f'  [{n:>3}/{len(routes)}] {a} -> {b}: ERROR {err}')
            continue
        results.append((body, data, ms))
        print(f'  [{n:>3}/{len(routes)}] {a:<20} -> {b:<20} {data["total_distance_miles"]:>7.0f} mi  '
              f'{len(data["fuel_stops"]):>2} stops  ${data["total_fuel_cost_usd"]:>7.2f}  '
              f'{ms:>7.0f} ms (server {data["meta"]["processing_ms"]:.0f} ms)')
    cold_wall = time.perf_counter() - cold_start

    cached_first = sum(1 for _, d, _ in results if d['meta']['cache_hit'])
    cold = [(d, ms) for _, d, ms in results if not d['meta']['cache_hit']]

    warm_ms, warm_server, warm_calls = [], [], 0
    warm_start = time.perf_counter()
    for body, _, _ in results:
        if limiter.over_budget():
            print('ORS call budget reached during warm pass (cache smaller than route count?).')
            break
        data, ms, err = request_route(session, api, body, limiter)
        if err:
            errors.append(('warm', '', err))
            continue
        warm_ms.append(ms)
        warm_server.append(data['meta']['processing_ms'])
        warm_calls += data['meta']['routing_api_calls']
    warm_wall = time.perf_counter() - warm_start

    map_ms = []
    map_start = time.perf_counter()
    for _, data, _ in results:
        started = time.perf_counter()
        resp = session.get(data['map_url'] if data['map_url'].startswith('http')
                           else args.base_url + data['map_url'], timeout=30)
        if resp.status_code == 200:
            map_ms.append((time.perf_counter() - started) * 1000)
        else:
            errors.append(('map', data['route_id'], resp.status_code))
    map_wall = time.perf_counter() - map_start

    cold_calls = sum(d['meta']['routing_api_calls'] for d, _ in cold)
    print('\n' + '=' * 100)
    summarize('1. New routes (cold, incl. ORS)', [ms for _, ms in cold])
    summarize('   server-side processing only', [d['meta']['processing_ms'] for d, _ in cold])
    summarize('2. Same routes again (cache hit)', warm_ms)
    summarize('   server-side processing only', warm_server)
    summarize('3. Map pages (HTML render)', map_ms)
    print('=' * 100)
    print(f'Routes OK: {len(results)}/{len(routes)}   errors: {len(errors)}   '
          f'cache hits during cold pass: {cached_first}')
    print(f'ORS calls: cold pass {cold_calls}, warm pass {warm_calls}')
    print(f'Wall clock: cold {cold_wall:.1f}s (includes rate-limiting pauses), '
          f'warm {warm_wall:.2f}s, maps {map_wall:.2f}s')
    if cold:
        net = [ms - d['meta']['processing_ms'] for d, ms in cold]
        print(f'Average ORS + network time per new route: {statistics.mean(net):.0f} ms; '
              f'our own processing: {statistics.mean(d["meta"]["processing_ms"] for d, _ in cold):.0f} ms')
    for err in errors[:10]:
        print('  error:', err)


if __name__ == '__main__':
    main()
