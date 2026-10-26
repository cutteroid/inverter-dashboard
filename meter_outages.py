"""Grid outages seen by the smart meter, kept for today and yesterday.

Two kinds:
  total    no grid at all: the meter reports every phase without voltage, or
           it went offline (it's powered by the grid) while the inverter
           doesn't see the grid either (see SmartMeter.check_offline)
  partial  some phases missing: without voltage on the grid side, or cut by
           the meter's relay (its protection tripped, e.g. at 256 V)

SmartMeter calls update() with the condition it sees now; a change closes
the open outage and/or opens a new one. A partial outage stays one entry while
any phase is missing (the phases seen missing add up); it ends when all are
back, or turns into a total outage. Outages under MIN_SECONDS are dropped. The
meter's wait before it switches the house back on after an outage (every phase
disconnected, RESTORE_SECONDS at most) is part of that outage. The open
outage is saved too, so a restart during an outage keeps its start. Entries
from before yesterday are dropped.
"""
import logging
import threading
from datetime import datetime, timedelta

from storage import atomic_write_json, load_json

logger = logging.getLogger(__name__)

TOTAL = "total"
PARTIAL = "partial"
# Shorter ones aren't kept: the meter sends V1, V2, V3 one by one, so a change
# of all phases passes through a split second of "some phases missing"
MIN_SECONDS = 10
# After the grid returns, the meter keeps every phase disconnected for a while
# before switching the house back on (seen live: 110 s). A partial outage with
# every phase cut and voltage on all of them ("restore") that starts as a total
# one ends is added to it if it lasts up to this long; a longer one is the
# meter's protection keeping the house off, and an entry of its own.
RESTORE_SECONDS = 300
JOIN_GAP_SECONDS = 10


def _iso(dt):
    return dt.isoformat(timespec="seconds")


class OutageLog:
    def __init__(self, path=None, clock=datetime.now):
        self.path = path   # None: in memory only (demo meter)
        self.clock = clock
        self._lock = threading.Lock()
        self._events = []  # closed outages, oldest first: {type, start, end, phases, cut}
        self._open = None  # the outage going on now, same keys without "end"
        self._load()

    def update(self, condition, missing=(), cut=(), at=None, restore=False):
        """The grid condition now: "ok", PARTIAL (missing: phase numbers, cut: the
        ones the meter's relay switched off; restore: every phase has voltage and
        every one is cut) or TOTAL. at: when it started (for an outage noticed
        late, e.g. the meter's offline time); default now."""
        at = at or self.clock()
        changed = False
        with self._lock:
            current = self._open
            if current and current["type"] == condition == PARTIAL:
                phases = sorted(set(current["phases"]) | set(missing))
                cut_phases = sorted(set(current["cut"]) | set(cut))
                if phases != current["phases"] or cut_phases != current["cut"]:
                    current["phases"], current["cut"] = phases, cut_phases
                    changed = True
                if current.get("restore") and set(missing) - set(cut):
                    # A phase without voltage: not just the meter's wait any more (its
                    # relays switching back on one by one keep it one)
                    current["restore"] = False
                    changed = True
            elif not current or current["type"] != condition:
                if current:
                    start = datetime.fromisoformat(current["start"])
                    ended = dict(current, end=_iso(max(at, start)))
                    if not self._joins_previous(ended, at) and (at - start).total_seconds() >= MIN_SECONDS:
                        self._events.append(ended)
                    self._open = None
                    changed = True
                if condition in (TOTAL, PARTIAL):
                    self._open = {"type": condition, "start": _iso(at),
                                  "phases": sorted(set(missing)) if condition == PARTIAL else [],
                                  "cut": sorted(set(cut)) if condition == PARTIAL else []}
                    if condition == PARTIAL and restore:
                        self._open["restore"] = True
                    changed = True
            if changed:
                self._prune()
        if changed:
            self._save()

    def snapshot(self):
        """Outages that started or ended today or yesterday, newest first; the open
        one first with "end": None."""
        with self._lock:
            self._prune()
            events = list(self._events)
            current = self._open
            if current and self._restoring_after(current, self.clock()):
                events[-1] = dict(events[-1], end=None)   # still the outage: the meter's wait
                current = None
            if current:
                events.append(dict(current, end=None))
        return sorted(({k: v for k, v in e.items() if k != "restore"} for e in events),
                      key=lambda e: e["start"], reverse=True)

    def _restoring_after(self, outage, until):
        """Is this the meter's wait after the last (total) outage, as of until?
        (caller holds the lock)"""
        if not outage.get("restore") or not self._events or self._events[-1]["type"] != TOTAL:
            return False
        start = datetime.fromisoformat(outage["start"])
        gap = (start - datetime.fromisoformat(self._events[-1]["end"])).total_seconds()
        return 0 <= gap <= JOIN_GAP_SECONDS and (until - start).total_seconds() <= RESTORE_SECONDS

    def _joins_previous(self, ended, at):
        """Add the meter's wait to the outage before it (caller holds the lock)."""
        if not self._restoring_after(ended, at):
            return False
        self._events[-1]["end"] = ended["end"]
        return True

    @property
    def open_type(self):
        with self._lock:
            return self._open["type"] if self._open else None

    def _prune(self):
        """Drop outages that ended before yesterday (caller holds the lock)."""
        since = _iso(datetime.combine(self.clock().date() - timedelta(days=1), datetime.min.time()))
        self._events = [e for e in self._events if e["end"] >= since]

    def _save(self):
        if not self.path:
            return
        with self._lock:
            data = {"events": list(self._events), "open": self._open}
        try:
            atomic_write_json(self.path, data)
        except OSError:
            logger.exception("Could not save the meter outage log")

    def _load(self):
        if not self.path:
            return
        data = load_json(self.path, {})
        if not isinstance(data, dict):
            return

        def valid(e, closed):
            try:
                datetime.fromisoformat(e["start"])
                if closed:
                    datetime.fromisoformat(e["end"])
                return e["type"] in (TOTAL, PARTIAL) and isinstance(e.get("phases"), list) \
                    and isinstance(e.get("cut"), list)
            except (KeyError, TypeError, ValueError):
                return False

        self._events = sorted((e for e in data.get("events") or [] if isinstance(e, dict) and valid(e, True)),
                              key=lambda e: e["start"])
        open_ = data.get("open")
        self._open = open_ if isinstance(open_, dict) and valid(open_, False) else None
        with self._lock:
            self._prune()
