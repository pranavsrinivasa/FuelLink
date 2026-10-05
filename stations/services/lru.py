import threading
import time
from collections import OrderedDict


class LRUCache:
    def __init__(self, max_entries, ttl_seconds=None):
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, stamp = item
            if self.ttl and time.monotonic() - stamp > self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (value, time.monotonic())
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()

    def __len__(self):
        return len(self._data)
