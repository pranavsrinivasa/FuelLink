import csv
import math
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from stations.models import Station
from stations.services.gazetteer import Gazetteer, popularity_scores
from stations.services.routing import OpenRouteServiceClient, ProviderUnavailable, RoutingError

ORS_GEOCODE_PAUSE = 0.7
# A precise hit this far from the city centroid is likely the wrong place, so it is rejected.
MAX_PRECISE_DRIFT_MILES = 25


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 3958.8 * math.asin(math.sqrt(a))


def read_rows(path):
    by_id = {}
    with open(path, newline='', encoding='utf-8-sig') as fh:
        for raw in csv.DictReader(fh):
            if not raw or not raw.get('OPIS Truckstop ID', '').strip():
                continue
            try:
                row = {
                    'opis_id': int(raw['OPIS Truckstop ID']),
                    'name': raw['Truckstop Name'].strip(),
                    'address': raw['Address'].strip(),
                    'city': raw['City'].strip(),
                    'state': raw['State'].strip().upper(),
                    'price': float(raw['Retail Price']),
                }
            except (ValueError, KeyError, TypeError):
                continue
            cur = by_id.get(row['opis_id'])
            if cur is None or row['price'] < cur['price']:
                if cur is not None and len(cur['name']) > len(row['name']):
                    row['name'] = cur['name']
                by_id[row['opis_id']] = row
    return list(by_id.values())


class Command(BaseCommand):
    help = 'Load fuel stations from the CSV and geocode them (tiered).'

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=str(settings.STATIONS_CSV))
        parser.add_argument('--top-x', type=int, default=settings.GEOCODE_TOP_X,
                            help='How many of the most-used stations to geocode precisely.')
        parser.add_argument('--skip-geocode', action='store_true',
                            help='Do not call ORS at all (city-level locations only).')

    def handle(self, *args, **opts):
        rows = read_rows(opts['csv'])
        self.stdout.write(f'{len(rows)} unique stations in CSV')
        scores = popularity_scores(rows)
        gaz = Gazetteer(settings.CENSUS_PLACES_FILE, settings.CENSUS_COUSUBS_FILE)
        us_states = gaz.states
        non_us = [r for r in rows if r['state'] not in us_states]
        rows = [r for r in rows if r['state'] in us_states]
        if non_us:
            self.stdout.write(f'{len(non_us)} stations outside the USA skipped '
                              f'(e.g. {sorted({r["state"] for r in non_us})[:6]})')

        previous = {s['opis_id']: s for s in Station.objects.filter(
            geo_precision__in=[Station.PRECISION_ADDRESS, Station.PRECISION_CITY_GEOCODED]
        ).values('opis_id', 'lat', 'lon', 'geo_precision')}

        use_ors = bool(settings.ORS_API_KEY) and not opts['skip_geocode']
        client = OpenRouteServiceClient() if use_ors else None
        if not use_ors:
            self.stdout.write(self.style.WARNING(
                'ORS not used: tier 1 (precise) and tier 3 (geocoded cities) are skipped.'))

        records, unmatched = [], []
        counts = {'address': 0, 'city': 0, 'city_fuzzy': 0, 'city_geocoded': 0}
        for row in rows:
            prev = previous.get(row['opis_id'])
            if prev:
                lat, lon, prec = prev['lat'], prev['lon'], prev['geo_precision']
            elif (hit := gaz.exact(row['city'], row['state'])):
                (lat, lon), prec = hit, Station.PRECISION_CITY
            elif (hit := gaz.fuzzy(row['city'], row['state'])):
                (lat, lon), prec = hit, Station.PRECISION_CITY_FUZZY
            else:
                lat = lon = prec = None
            row['lat'], row['lon'], row['geo_precision'] = lat, lon, prec
            row['popularity'] = scores[row['opis_id']]
            (unmatched if prec is None else records).append(row)

        still_unmatched = []
        for row in unmatched:
            result = None
            if client:
                try:
                    result = client.geocode(f"{row['city']}, {row['state']}", layers='locality')
                    time.sleep(ORS_GEOCODE_PAUSE)
                except ProviderUnavailable as exc:
                    self.stdout.write(self.style.WARNING(f'Stopping tier 3: {exc.message}'))
                    client = None
            if result and (not result.region or result.region == row['state']):
                row.update(lat=result.lat, lon=result.lon,
                           geo_precision=Station.PRECISION_CITY_GEOCODED)
                records.append(row)
            else:
                still_unmatched.append(row)

        with transaction.atomic():
            Station.objects.all().delete()
            Station.objects.bulk_create([Station(**r) for r in records], batch_size=1000)

        if client and opts['top_x'] > 0:
            self._geocode_top(client, records, opts['top_x'])

        for s in Station.objects.values_list('geo_precision', flat=True):
            counts[s] = counts.get(s, 0) + 1
        out = settings.BASE_DIR / 'data' / 'unmatched_stations.csv'
        if still_unmatched:
            with open(out, 'w', newline='') as fh:
                w = csv.DictWriter(fh, fieldnames=['opis_id', 'name', 'city', 'state'],
                                   extrasaction='ignore')
                w.writeheader()
                w.writerows(still_unmatched)
        self.stdout.write(self.style.SUCCESS(
            f'Loaded {Station.objects.count()} stations {counts}; '
            f'{len(still_unmatched)} unmatched (dropped)'
            + (f', listed in {out}' if still_unmatched else '')))

    def _geocode_top(self, client, records, top_x):
        top = sorted(records, key=lambda r: r['popularity'], reverse=True)[:top_x]
        todo = [r for r in top if r['geo_precision'] != Station.PRECISION_ADDRESS]
        self.stdout.write(f'Precisely geocoding {len(todo)} of the top {top_x} stations ...')
        done = 0
        for r in todo:
            try:
                res = client.geocode(f"{r['name']}, {r['city']}, {r['state']}",
                                     layers='venue,address')
            except RoutingError as exc:
                self.stdout.write(self.style.WARNING(
                    f'Stopped after {done}: {exc.message}. Re-run later to resume.'))
                return
            time.sleep(ORS_GEOCODE_PAUSE)
            if not res or (res.region and res.region != r['state']):
                continue
            if haversine(r['lat'], r['lon'], res.lat, res.lon) > MAX_PRECISE_DRIFT_MILES:
                continue
            Station.objects.filter(opis_id=r['opis_id']).update(
                lat=res.lat, lon=res.lon, geo_precision=Station.PRECISION_ADDRESS)
            done += 1
        self.stdout.write(f'  precise: {done}/{len(todo)}')
