import numpy as np
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from unittest.mock import patch

from stations.models import Station
from stations.services import planner
from stations.services.index import reset_index
from stations.services.lru import LRUCache
from stations.services.optimizer import Candidate, InfeasibleRoute, plan_fuel_stops
from stations.services.routing import RawRoute
from stations.services.spatial import cumulative_miles, resample_route

RANGE, MPG = 450.0, 10.0


class OptimizerTests(TestCase):
    def test_short_trip_needs_no_stop(self):
        plan = plan_fuel_stops([Candidate(0, 100, 3.0)], 300, RANGE, MPG)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, 0)

    def test_buys_only_what_is_needed_before_cheaper_station(self):
        plan = plan_fuel_stops([Candidate(0, 200, 4.0), Candidate(1, 300, 3.0)], 600, RANGE, MPG)
        self.assertEqual([s.candidate.index for s in plan.stops], [1])
        self.assertAlmostEqual(plan.stops[0].gallons, 15.0)
        self.assertAlmostEqual(plan.total_cost, 45.0)

    def test_never_exceeds_range_between_stops(self):
        rng = np.random.default_rng(1)
        miles = np.sort(rng.uniform(10, 2900, 120))
        cands = [Candidate(i, float(m), float(rng.uniform(2.8, 4.5)), float(rng.uniform(0, 4)))
                 for i, m in enumerate(miles)]
        plan = plan_fuel_stops(cands, 3000, RANGE, MPG)
        for s in plan.stops:
            self.assertGreaterEqual(s.arrival_fuel_miles, -1e-6)
            self.assertLessEqual(s.fuel_after_miles, RANGE + 1e-6)

    def test_infeasible_gap_raises(self):
        with self.assertRaises(InfeasibleRoute):
            plan_fuel_stops([Candidate(0, 100, 3.0)], 2000, RANGE, MPG)

    def test_prefers_cheaper_station_when_reachable(self):
        cands = [Candidate(0, 100, 5.0), Candidate(1, 350, 2.0)]
        plan = plan_fuel_stops(cands, 800, RANGE, MPG)
        self.assertEqual([s.candidate.index for s in plan.stops], [1])


class LRUTests(TestCase):
    def test_evicts_least_recently_used(self):
        c = LRUCache(2)
        c.set('a', 1); c.set('b', 2); c.get('a'); c.set('c', 3)
        self.assertIsNone(c.get('b'))
        self.assertEqual(c.get('a'), 1)


class SpatialTests(TestCase):
    def test_resample_matches_total_distance(self):
        pts = [(40.0, -100.0), (40.0, -99.0), (40.0, -98.0)]
        r = resample_route(pts, 150.0)
        self.assertAlmostEqual(r.total_miles, 150.0)
        self.assertGreater(len(r.lats), 100)
        self.assertAlmostEqual(cumulative_miles(np.array([0., 1.]), np.array([0., 0.]))[-1], 69.09, delta=0.2)


class FakeClient:
    calls = {'directions': 0, 'geocode': 0}
    A, B = (41.88, -87.63), (34.05, -118.24)

    def geocode(self, text, **kw):
        FakeClient.calls['geocode'] += 1
        from stations.services.routing import GeocodeResult
        lat, lon = self.A if 'chicago' in text.lower() else self.B
        return GeocodeResult(lat, lon, text.title())

    def directions(self, start, end):
        FakeClient.calls['directions'] += 1
        t = np.linspace(0, 1, 400)
        pts = [(start[0] + (end[0] - start[0]) * x, start[1] + (end[1] - start[1]) * x) for x in t]
        return RawRoute(points=pts, distance_miles=2015.0)


@override_settings(RESERVE_MILES=50.0, START_FUEL_COST_MODE='free')
class ApiTests(TestCase):
    def setUp(self):
        reset_index(); planner.reset_caches()
        FakeClient.calls = {'directions': 0, 'geocode': 0}
        a, b = FakeClient.A, FakeClient.B
        for i, t in enumerate(np.linspace(0.02, 0.98, 40)):
            Station.objects.create(
                opis_id=i + 1, name=f'STOP {i}', address='I-80', city='X', state='IL',
                price=3.0 + (i % 7) * 0.15, lat=a[0] + (b[0] - a[0]) * t, lon=a[1] + (b[1] - a[1]) * t,
                geo_precision='address')
        patcher = patch.object(planner, 'OpenRouteServiceClient', FakeClient)
        patcher.start(); self.addCleanup(patcher.stop)
        self.client = APIClient()

    def post(self, body):
        return self.client.post('/api/route/', body, format='json')

    def test_full_response_shape(self):
        res = self.post({'start': 'Chicago, IL', 'finish': 'Los Angeles, CA'})
        self.assertEqual(res.status_code, 200, res.content)
        d = res.json()
        self.assertGreater(len(d['fuel_stops']), 2)
        self.assertGreater(d['total_fuel_cost_usd'], 0)
        self.assertIn('assumptions', d)
        self.assertEqual(d['assumptions']['mpg'], 10.0)
        self.assertEqual(d['route_geojson']['type'], 'LineString')
        self.assertTrue(d['map_url'].endswith(f"/api/route/map/{d['route_id']}/"))
        self.assertAlmostEqual(sum(s['cost_usd'] for s in d['fuel_stops']), d['total_fuel_cost_usd'], places=1)
        self.assertLessEqual(d['total_gallons_purchased'], 2015 / 10 + 5)
        self.assertEqual(d['meta']['routing_api_calls'], 3)

    def test_second_request_is_a_cache_hit_with_zero_calls(self):
        self.post({'start': 'Chicago, IL', 'finish': 'Los Angeles, CA'})
        res = self.post({'start': 'chicago, il', 'finish': 'los angeles, ca'})
        d = res.json()
        self.assertTrue(d['meta']['cache_hit'])
        self.assertEqual(d['meta']['routing_api_calls'], 0)
        self.assertEqual(FakeClient.calls['directions'], 1)

    def test_coordinates_need_only_one_call(self):
        res = self.post({'start': {'lat': 41.88, 'lon': -87.63}, 'finish': '34.05,-118.24'})
        self.assertEqual(res.json()['meta']['routing_api_calls'], 1)

    def test_map_page_renders(self):
        d = self.post({'start': 'Chicago, IL', 'finish': 'Los Angeles, CA'}).json()
        page = self.client.get(f"/api/route/map/{d['route_id']}/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'leaflet')

    def test_validation_and_outside_usa(self):
        self.assertEqual(self.post({'start': 'Chicago'}).status_code, 400)
        self.assertEqual(self.post({'start': {'lat': 51.5, 'lon': -0.12}, 'finish': 'Chicago'}).status_code, 400)

    def test_short_trip_has_no_stops(self):
        class Short(FakeClient):
            def directions(self, s, e):
                return RawRoute(points=[s, e], distance_miles=120.0)
        with patch.object(planner, 'OpenRouteServiceClient', Short):
            d = self.post({'start': '41.0,-90.0', 'finish': '41.5,-89.0'}).json()
        self.assertEqual(d['fuel_stops'], [])
        self.assertEqual(d['total_fuel_cost_usd'], 0)

    def test_no_stations_gives_422_for_long_route(self):
        Station.objects.all().delete(); reset_index(); planner.reset_caches()
        res = self.post({'start': 'Chicago, IL', 'finish': 'Los Angeles, CA'})
        self.assertEqual(res.status_code, 422)
        self.assertIn('error', res.json())
