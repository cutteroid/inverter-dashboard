"""Recent history of a few readings, for the sparklines on the cards.

Each inverter poll adds its readings to a 5-minute bucket; a bucket keeps their
average, and the last 24 hours of buckets are kept. The file is written when a
bucket closes (every 5 minutes, not on every poll), so the history survives
restarts and deploys without wearing the disk.
"""
import logging
import threading
import time

from storage import atomic_write_json, load_json

logger = logging.getLogger(__name__)

BUCKET_SECONDS = 300
KEEP_SECONDS = 24 * 3600

# Series name -> reading field (see inverters/base.py) and decimals kept
SERIES = {
    "soc": ("battery_soc", 1),
    "pv": ("pv_total_power", 0),
    "load": ("load_power", 0),
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class PowerHistory:
    """5-minute averages of battery charge, solar and load over the last 24 hours."""

    def __init__(self, path=None, bucket_seconds=BUCKET_SECONDS, keep_seconds=KEEP_SECONDS):
        self.path = path
        self.bucket_seconds = bucket_seconds
        self.keep_seconds = keep_seconds
        self._lock = threading.Lock()
        self._buckets = []     # closed buckets, oldest first: [start, soc, pv, load] (None: no reading)
        self._current = None   # the open bucket: start and per-series sums and counts
        self._load()

    def add(self, readings, now=None):
        """Add one poll's readings (a dict with the driver's fields)."""
        now = time.time() if now is None else now
        start = int(now // self.bucket_seconds * self.bucket_seconds)
        closed = False
        with self._lock:
            if self._current and self._current["start"] != start:
                self._buckets.append(self._average(self._current))
                self._current = None
                closed = True
            if self._current is None:
                self._current = {"start": start, "sums": dict.fromkeys(SERIES, 0.0),
                                 "counts": dict.fromkeys(SERIES, 0)}
            for name, (field, _) in SERIES.items():
                value = readings.get(field)
                if _number(value):
                    self._current["sums"][name] += value
                    self._current["counts"][name] += 1
            self._trim(now)
        if closed:
            self._save()

    def series(self, now=None):
        """The kept buckets plus the open one, as parallel lists:
        {"bucket_seconds", "now", "t": [bucket start, ...], "soc": [...], "pv": [...], "load": [...]}."""
        now = time.time() if now is None else now
        with self._lock:
            buckets = [b for b in self._buckets if b[0] > now - self.keep_seconds]
            if self._current:
                buckets.append(self._average(self._current))
        result = {"bucket_seconds": self.bucket_seconds, "now": int(now), "t": [b[0] for b in buckets]}
        for i, name in enumerate(SERIES, 1):
            result[name] = [b[i] for b in buckets]
        return result

    def _average(self, bucket):
        values = [bucket["start"]]
        for name, (_, decimals) in SERIES.items():
            count = bucket["counts"][name]
            values.append(round(bucket["sums"][name] / count, decimals) if count else None)
        return values

    def _trim(self, now):
        cutoff = now - self.keep_seconds
        while self._buckets and self._buckets[0][0] <= cutoff:
            self._buckets.pop(0)

    def _save(self):
        if not self.path:
            return
        with self._lock:
            data = {"bucket_seconds": self.bucket_seconds, "buckets": list(self._buckets)}
        try:
            atomic_write_json(self.path, data)
        except OSError:
            logger.exception("Failed to save the power history")

    def _load(self):
        if not self.path:
            return
        data = load_json(self.path, {})
        if not isinstance(data, dict) or data.get("bucket_seconds") != self.bucket_seconds:
            return
        width = 1 + len(SERIES)
        buckets = [b for b in data.get("buckets") or []
                   if isinstance(b, list) and len(b) == width and _number(b[0])
                   and all(v is None or _number(v) for v in b[1:])]
        buckets.sort(key=lambda b: b[0])
        self._buckets = buckets
        self._trim(time.time())
