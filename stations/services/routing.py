from dataclasses import dataclass

import polyline as polyline_codec
import requests
from django.conf import settings


class RoutingError(Exception):
    status_code = 502

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class NotConfigured(RoutingError):
    status_code = 503


class LocationNotFound(RoutingError):
    status_code = 400


class RouteNotFound(RoutingError):
    status_code = 422


class ProviderUnavailable(RoutingError):
    status_code = 502


@dataclass
class GeocodeResult:
    lat: float
    lon: float
    label: str
    layer: str = ''
    region: str = ''


@dataclass
class RawRoute:
    points: list
    distance_miles: float


class OpenRouteServiceClient:
    def __init__(self, api_key=None, base_url=None, timeout=None, session=None):
        self.api_key = api_key if api_key is not None else settings.ORS_API_KEY
        self.base_url = (base_url or settings.ORS_BASE_URL).rstrip('/')
        self.timeout = timeout or settings.ORS_TIMEOUT_SECONDS
        self.session = session or requests.Session()

    def _headers(self):
        if not self.api_key:
            raise NotConfigured(
                'ORS_API_KEY is not set. Get a free key at https://openrouteservice.org '
                'and put it in .env')
        return {'Authorization': self.api_key, 'Content-Type': 'application/json'}

    def _request(self, method, path, **kwargs):
        try:
            resp = self.session.request(method, f'{self.base_url}{path}', headers=self._headers(),
                                        timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ProviderUnavailable(f'Routing provider unreachable: {exc}') from exc
        if resp.status_code == 429:
            raise ProviderUnavailable('Routing provider rate limit reached, try again shortly.')
        if resp.status_code in (401, 403):
            raise NotConfigured('ORS rejected the API key (check ORS_API_KEY).')
        return resp

    def geocode(self, text, layers=None, size=1):
        params = {'text': text, 'boundary.country': 'US', 'size': size}
        if layers:
            params['layers'] = layers
        resp = self._request('GET', '/geocode/search', params=params)
        if resp.status_code >= 500:
            raise ProviderUnavailable('Routing provider geocoder is unavailable.')
        if resp.status_code != 200:
            return None
        features = resp.json().get('features') or []
        if not features:
            return None
        feat = features[0]
        lon, lat = feat['geometry']['coordinates']
        props = feat.get('properties', {})
        return GeocodeResult(lat=lat, lon=lon, label=props.get('label', text),
                             layer=props.get('layer', ''), region=props.get('region_a', ''))

    def directions(self, start, end):
        body = {
            'coordinates': [[start[1], start[0]], [end[1], end[0]]],
            'units': 'mi',
            'instructions': False,
            'radiuses': [-1, -1],
        }
        resp = self._request('POST', '/v2/directions/driving-car', json=body)
        if resp.status_code >= 500:
            raise ProviderUnavailable('Routing provider is unavailable.')
        if resp.status_code != 200:
            try:
                msg = resp.json().get('error', {}).get('message', resp.text)
            except ValueError:
                msg = resp.text
            raise RouteNotFound(f'No drivable route found between the two locations: {msg}')
        route = resp.json()['routes'][0]
        return RawRoute(points=polyline_codec.decode(route['geometry']),
                        distance_miles=float(route['summary']['distance']))
