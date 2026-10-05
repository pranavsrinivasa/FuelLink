import csv
import re
from collections import Counter

from rapidfuzz import fuzz, process

_SUFFIXES = {
    'city', 'town', 'village', 'cdp', 'borough', 'municipality', 'township',
    'comunidad', 'zona', 'urbana', 'metropolitan', 'government', 'balance',
    'consolidated', 'unified', 'county', 'plantation', 'ccd', 'twp', 'unorganized',
}
_ABBREVIATIONS = {'st': 'saint', 'ste': 'sainte', 'ft': 'fort', 'mt': 'mount', 'n': 'north',
                  's': 'south', 'e': 'east', 'w': 'west'}

CHAIN_WEIGHTS = {
    'pilot': 3.0, 'flying j': 3.0, "love's": 3.0, 'loves': 3.0, 'travelcenters': 3.0,
    'petro': 2.5, ' ta ': 2.5, 'travel centers of america': 3.0, 'sapp bros': 2.0,
    'buc-ee': 2.5, 'kwik trip': 2.0, 'casey': 1.0, 'speedway': 1.0, 'circle k': 1.0,
}
_INTERSTATE = re.compile(r'\bI-\d+', re.I)


def normalize_city(name):
    name = re.sub(r"[.'`]", '', name.lower())
    name = re.sub(r'[^a-z0-9 ]', ' ', name)
    words = [_ABBREVIATIONS.get(w, w) for w in name.split()]
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return ' '.join(words)


class Gazetteer:

    def __init__(self, *paths):
        self._by_state = {}
        for path in paths:
            self._load(path)

    @property
    def states(self):
        return set(self._by_state)

    def _load(self, path):
        loaded = {}
        with open(path, encoding='utf-8') as fh:
            reader = csv.reader(fh, delimiter='\t')
            header = [h.strip() for h in next(reader)]
            ix = {h: i for i, h in enumerate(header)}
            for row in reader:
                if len(row) < len(header):
                    continue
                state = row[ix['USPS']].strip()
                key = normalize_city(row[ix['NAME']])
                area = float(row[ix['ALAND_SQMI']] or 0)
                lat, lon = float(row[ix['INTPTLAT']]), float(row[ix['INTPTLONG']])
                bucket = loaded.setdefault(state, {})
                if key not in bucket or area > bucket[key][2]:
                    bucket[key] = (lat, lon, area)
        for state, bucket in loaded.items():
            target = self._by_state.setdefault(state, {})
            for key, value in bucket.items():
                target.setdefault(key, value)

    def exact(self, city, state):
        hit = self._by_state.get(state, {}).get(normalize_city(city))
        return (hit[0], hit[1]) if hit else None

    def fuzzy(self, city, state, threshold=88):
        bucket = self._by_state.get(state)
        if not bucket:
            return None
        match = process.extractOne(normalize_city(city), bucket.keys(), scorer=fuzz.ratio,
                                   score_cutoff=threshold)
        if not match:
            return None
        hit = bucket[match[0]]
        return hit[0], hit[1]


def popularity_scores(rows):
    # The CSV has no traffic data, so "most used" is a heuristic: major chains, interstate exits, repeat listings.
    listings = Counter(r['opis_id'] for r in rows)
    scores = {}
    for r in rows:
        text = f" {r['name']} ".lower()
        score = max([w for k, w in CHAIN_WEIGHTS.items() if k in text] or [0.0])
        if _INTERSTATE.search(r['address']):
            score += 2.0
        score += 0.1 * (listings[r['opis_id']] - 1)
        scores[r['opis_id']] = score
    return scores
