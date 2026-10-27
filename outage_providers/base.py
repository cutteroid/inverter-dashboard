"""Base classes and factory for outage schedule providers."""
import time
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# A schedule older than this (because fetches keep failing) is no longer shown.
SCHEDULE_MAX_AGE = timedelta(minutes=30)


class OutageFetchError(Exception):
    """Provider could not get a schedule (HTTP error, bad config, stale data).

    Distinct from an empty window list, which means "no outages today".
    """


class OutageProvider:
    """Base class for outage schedule providers."""

    display_name = "енергопостачальник"

    def fetch_windows(self):
        """Return list of (start_h, start_m, end_h, end_m) for today.

        Raise OutageFetchError when the schedule can't be obtained; an empty
        list means no outages today. Override in subclass.
        """
        raise NotImplementedError

    def fetch_days(self):
        """Return {date: windows} for today and, once published, tomorrow.

        Today must be present. Tomorrow is omitted while its schedule is not
        known. Providers that only know today can rely on this default.
        """
        return {datetime.now().date(): self.fetch_windows()}


def create_outage_provider(provider_name, **kwargs):
    """Create an outage provider by name.

    Args:
        provider_name: "lvivoblenergo", "yasno", "dtek", or "none" to disable.
        **kwargs: Provider-specific keyword arguments.

    Returns:
        An OutageProvider instance, or None if provider_name is "none".
    """
    from outage_providers.lvivoblenergo import LvivoblenergoProvider
    from outage_providers.yasno import YasnoProvider
    from outage_providers.dtek import DtekProvider

    if provider_name == "none":
        return None
    if provider_name == "lvivoblenergo":
        return LvivoblenergoProvider(group=kwargs.get("group"))
    if provider_name == "yasno":
        return YasnoProvider(
            group=kwargs.get("group"),
            region_id=int(kwargs.get("region_id", 25)),
            dso_id=int(kwargs.get("dso_id", 902)),
        )
    if provider_name == "dtek":
        return DtekProvider(
            group=kwargs.get("group"),
            region=kwargs.get("dtek_region"),
        )
    raise ValueError(f"Unknown outage provider: {provider_name}")


def window_datetimes(day, window):
    """Convert a (start_h, start_m, end_h, end_m) window on `day` to datetimes.

    An end of 24:00 becomes midnight of the next day.
    """
    sh, sm, eh, em = window
    midnight = datetime.combine(day, datetime.min.time())
    start_dt = midnight.replace(hour=sh, minute=sm)
    if eh == 24:
        end_dt = datetime.combine(day + timedelta(days=1), datetime.min.time())
    else:
        end_dt = midnight.replace(hour=eh, minute=em)
    return start_dt, end_dt


class OutageSchedulePoller:
    """Polls an outage provider and stores today's outage schedule."""

    def __init__(self, provider=None, group=None, poll_interval=60):
        # Backward compat: if no provider given, create Lvivoblenergo
        if provider is None:
            from outage_providers.lvivoblenergo import LvivoblenergoProvider
            provider = LvivoblenergoProvider(group=group)
        self.provider = provider
        self.poll_interval = poll_interval
        self._windows = []  # today: list of (start_hour, start_min, end_hour, end_min)
        self._days = {}  # {date: windows} for today and (if published) tomorrow
        self._last_updated = None  # time of the last successful fetch
        self._last_error = None  # message from the last failed fetch, if any
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def _fetch_schedule(self):
        """Fetch and parse the schedule from the provider."""
        provider_name = type(self.provider).__name__
        logger.info("Fetching outage schedule from %s", provider_name)
        try:
            days = self.provider.fetch_days()
        except OutageFetchError as e:
            logger.warning("Outage schedule unavailable from %s: %s", provider_name, e)
            with self._lock:
                self._last_error = str(e)
            return
        except Exception as e:
            logger.exception("Error fetching outage schedule from %s", provider_name)
            with self._lock:
                self._last_error = f"{type(e).__name__}: {e}"
            return
        now = datetime.now()
        if now.date() not in days:
            # Fetched just before midnight: no schedule for the new day in it, and
            # that isn't "no outages". Keep the last one (it may have this day as
            # its "tomorrow") until the next fetch.
            logger.info("Outage schedule from %s has no entry for %s yet", provider_name, now.date())
            return
        windows = days[now.date()]
        with self._lock:
            self._windows = windows
            self._days = days
            self._last_updated = now
            self._last_error = None
        logger.info("Outage schedule updated: %d windows today %s, tomorrow %s",
                    len(windows), windows,
                    days.get(now.date() + timedelta(days=1), "not published"))

    def get_outage_status(self):
        """Return current outage status.

        Returns a dict with:
          - status: "active", "upcoming", "clear", "unknown" (not fetched
            yet), or "error" (fetches failing and no current schedule)
          - error: str (if error)
          - end_time: datetime (if active)
          - remaining_minutes: int (if active)
          - upcoming_windows: list of (start_dt, end_dt) (if upcoming)
          - days: list of {"date", "windows": [(start_dt, end_dt)]} for today
            and, if published, tomorrow (unless unknown/error)
        """
        with self._lock:
            windows = list(self._windows)
            days = dict(self._days)
            last_updated = self._last_updated
            last_error = self._last_error

        now = datetime.now()
        today = now.date()

        # Only trust a recent schedule that has this day; otherwise a failed fetch
        # would read as "clear". Just after midnight that is the last fetch's
        # "tomorrow", until the providers publish the new day as today.
        if last_updated is not None and last_updated.date() != today:
            windows = days.get(today)
        if (last_updated is None or windows is None
                or now - last_updated > SCHEDULE_MAX_AGE):
            if last_error:
                return {"status": "error", "error": last_error}
            return {"status": "unknown"}

        active_start = None
        active_end = None
        upcoming = []

        for window in windows:
            start_dt, end_dt = window_datetimes(today, window)
            if start_dt <= now < end_dt:
                active_start = start_dt
                active_end = end_dt
            elif now < start_dt:
                upcoming.append((start_dt, end_dt))

        if active_end:
            remaining = (active_end - now).total_seconds() / 60
            result = {
                "status": "active",
                "start_time": active_start,
                "end_time": active_end,
                "remaining_minutes": int(remaining),
            }
        elif upcoming:
            # Find when the current electricity period started
            # (end of the most recent past outage window, or midnight)
            electricity_start = datetime.combine(today, datetime.min.time())
            for window in windows:
                _, edt = window_datetimes(today, window)
                if edt <= now:
                    electricity_start = edt
            result = {
                "status": "upcoming",
                "upcoming_windows": upcoming,
                "electricity_start": electricity_start,
            }
        else:
            result = {"status": "clear"}

        result["days"] = [
            {"date": day, "windows": [window_datetimes(day, w) for w in days[day]]}
            for day in (today, today + timedelta(days=1))
            if day in days
        ]
        return result

    def _run(self):
        while self._running:
            self._fetch_schedule()
            time.sleep(self.poll_interval)

    def start(self):
        """Start polling in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
