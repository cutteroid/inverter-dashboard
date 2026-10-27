"""Outage schedule provider for YASNO (DTEK regions)."""
import os
import logging
from datetime import date, timedelta

import requests

from outage_providers.base import OutageProvider, OutageFetchError

logger = logging.getLogger(__name__)


class YasnoProvider(OutageProvider):
    """Outage schedule provider for YASNO (DTEK regions)."""

    display_name = "YASNO"

    API_URL_TEMPLATE = (
        "https://app.yasno.ua/api/blackout-service/public/shutdowns"
        "/regions/{region_id}/dsos/{dso_id}/planned-outages"
    )

    def __init__(self, group=None, region_id=25, dso_id=902):
        self.group = group or os.environ.get("OUTAGE_GROUP", "2.1")
        self.region_id = region_id
        self.dso_id = dso_id

    def fetch_windows(self):
        """Fetch today's outage windows from the YASNO API."""
        return self.fetch_days()[date.today()]

    def fetch_days(self):
        """Fetch outage windows for today and, when scheduled, tomorrow."""
        url = self.API_URL_TEMPLATE.format(
            region_id=self.region_id, dso_id=self.dso_id
        )
        logger.info("YASNO: fetching %s (group=%s)", url, self.group)
        try:
            resp = requests.get(url, timeout=15)
        except requests.RequestException as e:
            raise OutageFetchError(f"YASNO request failed: {type(e).__name__}") from e
        if not resp.ok:
            body_snippet = resp.text[:200] if resp.text else "(empty)"
            raise OutageFetchError(
                f"YASNO API returned HTTP {resp.status_code} for region "
                f"{self.region_id} / DSO {self.dso_id}: {body_snippet}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise OutageFetchError("YASNO: the answer isn't JSON") from e
        group_data = data.get(self.group) if isinstance(data, dict) else None
        if not isinstance(group_data, dict) or not group_data:
            raise OutageFetchError(
                f"YASNO: group '{self.group}' not found in response "
                f"(available: {', '.join(list(data.keys())[:10])})"
            )

        today = date.today()
        today_data = group_data.get("today") or {}
        today_date = str(today_data.get("date", ""))
        # YASNO always returns a "tomorrow" entry, empty until it is scheduled,
        # so only treat it as published when it has outage slots.
        tomorrow_data = group_data.get("tomorrow") or {}
        if today_date and not today_date.startswith(today.isoformat()):
            # Just after midnight YASNO may still send yesterday as "today" and
            # this day as "tomorrow": use that while it has outage slots
            if str(tomorrow_data.get("date", "")).startswith(today.isoformat()):
                windows = self._definite_windows(tomorrow_data)
                if windows:
                    logger.info("YASNO: group %s today %s (still sent as tomorrow)", self.group, windows)
                    return {today: windows}
            raise OutageFetchError(f"YASNO: today's schedule isn't published yet ({today_date[:10]})")
        result = {today: self._definite_windows(today_data)}
        tomorrow = today + timedelta(days=1)
        if str(tomorrow_data.get("date", "")).startswith(tomorrow.isoformat()):
            tomorrow_windows = self._definite_windows(tomorrow_data)
            if tomorrow_windows:
                result[tomorrow] = tomorrow_windows
        logger.info("YASNO: group %s today %s, tomorrow %s",
                    self.group, result[today], result.get(tomorrow, "not published"))
        return result

    @staticmethod
    def _definite_windows(day_data):
        windows = []
        for slot in day_data.get("slots") or []:
            if not isinstance(slot, dict) or slot.get("type") != "Definite":
                continue
            try:
                sh, sm = divmod(int(slot["start"]), 60)
                eh, em = divmod(int(slot["end"]), 60)
            except (KeyError, TypeError, ValueError) as e:
                raise OutageFetchError("YASNO: unexpected slot in the schedule") from e
            windows.append((sh, sm, eh, em))
        return windows
