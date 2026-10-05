import hashlib
import threading
from dataclasses import dataclass

import numpy as np
from django.conf import settings

from stations.models import Station

_lock = threading.Lock()
_index = None

_CORRIDOR_BY_PRECISION = {
    Station.PRECISION_ADDRESS: lambda: settings.CORRIDOR_ADDRESS_MILES,
}


@dataclass
class StationIndex:
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray
    corridor: np.ndarray
    meta: list
    version: str

    def __len__(self):
        return len(self.meta)


def build_index():
    rows = list(Station.objects.order_by('id').values(
        'opis_id', 'name', 'address', 'city', 'state', 'price', 'lat', 'lon', 'geo_precision'))
    corridor_for = lambda p: _CORRIDOR_BY_PRECISION.get(p, lambda: settings.CORRIDOR_CITY_MILES)()
    lat = np.array([r['lat'] for r in rows], dtype=float)
    lon = np.array([r['lon'] for r in rows], dtype=float)
    price = np.array([float(r['price']) for r in rows], dtype=float)
    corridor = np.array([corridor_for(r['geo_precision']) for r in rows], dtype=float)
    meta = [{
        'opis_id': r['opis_id'], 'name': r['name'], 'address': r['address'],
        'city': r['city'], 'state': r['state'], 'location_precision': r['geo_precision'],
    } for r in rows]
    digest = hashlib.sha1(price.tobytes() + lat.tobytes() + lon.tobytes()).hexdigest()[:12]
    return StationIndex(lat, lon, price, corridor, meta, digest)


def get_index():
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = build_index()
    return _index


def reset_index():
    global _index
    with _lock:
        _index = None
