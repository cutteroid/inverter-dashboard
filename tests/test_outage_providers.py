"""Tests for outage provider factory, HTML parsing, and schedule status."""
import pytest
import requests
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

from outage_providers.base import (
    create_outage_provider, OutageProvider, OutageSchedulePoller, OutageFetchError,
    SCHEDULE_MAX_AGE,
)
from outage_providers.dtek import DtekProvider, slots_to_windows
from outage_providers.lvivoblenergo import LvivoblenergoProvider, parse_group_windows
from outage_providers.yasno import YasnoProvider


class TestCreateOutageProvider:
    def test_none_returns_none(self):
        assert create_outage_provider("none") is None

    def test_lvivoblenergo(self):
        provider = create_outage_provider("lvivoblenergo", group="4.1")
        assert isinstance(provider, LvivoblenergoProvider)
        assert provider.group == "4.1"

    def test_yasno(self):
        provider = create_outage_provider(
            "yasno", group="2.1", region_id="25", dso_id="902"
        )
        assert isinstance(provider, YasnoProvider)
        assert provider.group == "2.1"
        assert provider.region_id == 25
        assert provider.dso_id == 902

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown outage provider"):
            create_outage_provider("unknown_provider")


class TestParseGroupWindows:
    def test_single_window(self):
        html = "<p>Група 4.1. з 09:00 до 12:00</p>"
        windows = parse_group_windows(html, "4.1")
        assert windows == [(9, 0, 12, 0)]

    def test_multiple_windows(self):
        html = "<p>Група 4.1. з 09:00 до 12:00, з 18:00 до 21:00</p>"
        windows = parse_group_windows(html, "4.1")
        assert windows == [(9, 0, 12, 0), (18, 0, 21, 0)]

    def test_group_not_found(self):
        html = "<p>Група 3.2. з 09:00 до 12:00</p>"
        assert parse_group_windows(html, "4.1") is None

    def test_group_listed_without_outages(self):
        html = "<p>Група 4.1. Електроенергія є.</p>"
        assert parse_group_windows(html, "4.1") == []

    def test_empty_html(self):
        assert parse_group_windows("", "4.1") is None


class TestOutageSchedulePollerStatus:
    def _make_poller(self, windows, last_updated=None):
        """Helper to create a poller with pre-set state."""
        provider = LvivoblenergoProvider(group="4.1")
        poller = OutageSchedulePoller(provider=provider)
        poller._windows = windows
        poller._last_updated = last_updated
        return poller

    def test_unknown_when_never_fetched(self):
        poller = self._make_poller([], last_updated=None)
        status = poller.get_outage_status()
        assert status["status"] == "unknown"

    def test_active_during_window(self):
        # Fixed time: 12:30, inside window 12:00-13:00
        now = datetime.now().replace(hour=12, minute=30, second=0, microsecond=0)
        poller = self._make_poller(
            [(12, 0, 13, 0)],
            last_updated=now,
        )
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            status = poller.get_outage_status()
        assert status["status"] == "active"
        assert "remaining_minutes" in status
        assert status["remaining_minutes"] >= 0

    def test_upcoming_before_window(self):
        # Use a fixed time (10:00) so +2h doesn't cross midnight
        now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        poller = self._make_poller(
            [(12, 0, 13, 0)],  # window from 12:00-13:00
            last_updated=now,
        )
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            status = poller.get_outage_status()
        assert status["status"] == "upcoming"
        assert len(status["upcoming_windows"]) == 1

    def test_clear_all_past(self):
        # Fixed time: 15:00, past window 8:00-10:00
        now = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
        poller = self._make_poller(
            [(8, 0, 10, 0)],
            last_updated=now,
        )
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            status = poller.get_outage_status()
        assert status["status"] == "clear"

    def test_midnight_crossing(self):
        """Window (22,0,24,0) should create end_dt as next day midnight."""
        now = datetime.now().replace(hour=22, minute=30, second=0, microsecond=0)
        poller = self._make_poller(
            [(22, 0, 24, 0)],
            last_updated=now,
        )
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            status = poller.get_outage_status()
        assert status["status"] == "active"
        # end_time should be next day midnight
        assert status["end_time"].hour == 0
        assert status["end_time"].day == now.day + 1

    def test_electricity_start_tracks_last_ended_window(self):
        """electricity_start should be the end of the last past window."""
        # Fixed time: 12:00. Past window 8:00-11:00, future window 14:00-15:00
        now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        poller = self._make_poller(
            [
                (8, 0, 11, 0),   # past window (ended at 11:00)
                (14, 0, 15, 0),  # future window
            ],
            last_updated=now,
        )
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            status = poller.get_outage_status()
        assert status["status"] == "upcoming"
        assert "electricity_start" in status
        assert status["electricity_start"].hour == 11

    def test_multiple_upcoming(self):
        """All future windows should be returned in the upcoming list."""
        # Fixed time: 10:00. Two future windows at 14:00-15:00 and 18:00-19:00
        now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        poller = self._make_poller(
            [
                (14, 0, 15, 0),
                (18, 0, 19, 0),
            ],
            last_updated=now,
        )
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            status = poller.get_outage_status()
        assert status["status"] == "upcoming"
        assert len(status["upcoming_windows"]) == 2


class TestCreateDtekProvider:
    def test_dtek(self):
        provider = create_outage_provider("dtek", group="4.2", dtek_region="kyiv-region")
        assert isinstance(provider, DtekProvider)
        assert provider.group == "4.2"
        assert provider.region == "kyiv-region"
        assert provider.url.endswith("/data/kyiv-region.json")

    def test_dtek_defaults_to_kyiv_region(self):
        provider = create_outage_provider("dtek", group="1.1", dtek_region=None)
        assert provider.region == "kyiv-region"


class TestSlotsToWindows:
    @staticmethod
    def _slots(**overrides):
        """All 24 hours 'yes', with overrides keyed as h<N>."""
        slots = {str(h): "yes" for h in range(1, 25)}
        for key, val in overrides.items():
            slots[key[1:]] = val
        return slots

    def test_no_outages(self):
        assert slots_to_windows(self._slots()) == []

    def test_adjacent_hours_merge(self):
        # Slots 9-12 cover 08:00-12:00
        slots = self._slots(h9="no", h10="no", h11="no", h12="no")
        assert slots_to_windows(slots) == [(8, 0, 12, 0)]

    def test_half_hours(self):
        # second half of 18:00-19:00, then full 19-22, then first half of 22:00-23:00
        slots = self._slots(h19="second", h20="no", h21="no", h22="no", h23="first")
        assert slots_to_windows(slots) == [(18, 30, 22, 30)]

    def test_runs_to_midnight(self):
        slots = self._slots(h23="no", h24="no")
        assert slots_to_windows(slots) == [(22, 0, 24, 0)]

    def test_maybe_values_are_not_outages(self):
        slots = self._slots(h5="maybe", h6="mfirst", h7="msecond")
        assert slots_to_windows(slots) == []

    def test_multiple_windows(self):
        slots = self._slots(h1="no", h10="no", h11="no")
        assert slots_to_windows(slots) == [(0, 0, 1, 0), (9, 0, 11, 0)]


class TestDtekProviderFetch:
    @staticmethod
    def _ts(day):
        return str(int(datetime.combine(day, datetime.min.time()).timestamp()))

    @staticmethod
    def _slots(off_hours):
        slots = {str(h): "yes" for h in range(1, 25)}
        for h in off_hours:
            slots[str(h)] = "no"
        return slots

    def _feed(self, day=None, groups=None, ok=True, tomorrow_groups=None):
        day = day or datetime.now().date()
        ts = self._ts(day)
        if groups is None:
            groups = {"GPV4.2": self._slots([9])}
        data = {ts: groups}
        if tomorrow_groups is not None:
            data[self._ts(day + timedelta(days=1))] = tomorrow_groups
        return {
            "fact": {"data": data, "today": int(ts), "update": "17.10.2026 01:25"},
            "lastUpdateStatus": {"status": "parsed" if ok else "error", "ok": ok, "code": 200},
        }

    @staticmethod
    def _response(json_data=None, status_code=200):
        resp = MagicMock()
        resp.ok = status_code < 400
        resp.status_code = status_code
        resp.json.return_value = json_data
        return resp

    def test_returns_today_windows(self):
        provider = DtekProvider(group="4.2", region="kyiv-region")
        with patch("outage_providers.dtek.requests.get", return_value=self._response(self._feed())):
            assert provider.fetch_windows() == [(8, 0, 9, 0)]

    def test_accepts_gpv_prefixed_group(self):
        provider = DtekProvider(group="GPV4.2", region="kyiv-region")
        with patch("outage_providers.dtek.requests.get", return_value=self._response(self._feed())):
            assert provider.fetch_windows() == [(8, 0, 9, 0)]

    def test_http_error_raises(self):
        provider = DtekProvider(group="4.2")
        with patch("outage_providers.dtek.requests.get", return_value=self._response(status_code=503)):
            with pytest.raises(OutageFetchError, match="HTTP 503"):
                provider.fetch_windows()

    def test_network_error_raises(self):
        provider = DtekProvider(group="4.2")
        with patch("outage_providers.dtek.requests.get",
                   side_effect=requests.ConnectionError("down")):
            with pytest.raises(OutageFetchError, match="request failed"):
                provider.fetch_windows()

    def test_missing_group_raises(self):
        provider = DtekProvider(group="9.9")
        with patch("outage_providers.dtek.requests.get", return_value=self._response(self._feed())):
            with pytest.raises(OutageFetchError, match="group '9.9' not in"):
                provider.fetch_windows()

    def test_stale_feed_without_today_raises(self):
        yesterday = datetime.now().date() - timedelta(days=1)
        provider = DtekProvider(group="4.2")
        with patch("outage_providers.dtek.requests.get",
                   return_value=self._response(self._feed(day=yesterday))):
            with pytest.raises(OutageFetchError, match="no schedule for"):
                provider.fetch_windows()

    def test_scraper_error_still_uses_current_data(self):
        provider = DtekProvider(group="4.2")
        with patch("outage_providers.dtek.requests.get",
                   return_value=self._response(self._feed(ok=False))):
            assert provider.fetch_windows() == [(8, 0, 9, 0)]

    def test_days_without_tomorrow(self):
        today = datetime.now().date()
        provider = DtekProvider(group="4.2")
        with patch("outage_providers.dtek.requests.get", return_value=self._response(self._feed())):
            assert provider.fetch_days() == {today: [(8, 0, 9, 0)]}

    def test_days_with_tomorrow(self):
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        feed = self._feed(tomorrow_groups={"GPV4.2": self._slots([19, 20])})
        provider = DtekProvider(group="4.2")
        with patch("outage_providers.dtek.requests.get", return_value=self._response(feed)):
            assert provider.fetch_days() == {
                today: [(8, 0, 9, 0)],
                tomorrow: [(18, 0, 20, 0)],
            }

    def test_tomorrow_without_outages_is_published_empty(self):
        tomorrow = datetime.now().date() + timedelta(days=1)
        feed = self._feed(tomorrow_groups={"GPV4.2": self._slots([])})
        provider = DtekProvider(group="4.2")
        with patch("outage_providers.dtek.requests.get", return_value=self._response(feed)):
            assert provider.fetch_days()[tomorrow] == []

    def test_unknown_region_raises_on_fetch(self):
        provider = DtekProvider(group="4.2", region="lviv")
        with patch("outage_providers.dtek.requests.get") as mock_get:
            with pytest.raises(OutageFetchError, match="Unknown DTEK region"):
                provider.fetch_windows()
            mock_get.assert_not_called()


class TestFailedFetchIsNotClear:
    """A failed fetch must never be reported as "no outages today"."""

    class _FailingProvider(OutageProvider):
        display_name = "test"

        def fetch_windows(self):
            raise OutageFetchError("API returned HTTP 500")

    class _OkProvider(OutageProvider):
        display_name = "test"

        def __init__(self, windows):
            self.windows = windows

        def fetch_windows(self):
            return self.windows

    def test_error_status_when_first_fetch_fails(self):
        poller = OutageSchedulePoller(provider=self._FailingProvider())
        poller._fetch_schedule()
        status = poller.get_outage_status()
        assert status == {"status": "error", "error": "API returned HTTP 500"}

    def test_unexpected_exception_is_error(self):
        class Broken(OutageProvider):
            def fetch_windows(self):
                raise KeyError("slots")
        poller = OutageSchedulePoller(provider=Broken())
        poller._fetch_schedule()
        assert poller.get_outage_status()["status"] == "error"

    def test_empty_successful_fetch_is_clear(self):
        poller = OutageSchedulePoller(provider=self._OkProvider([]))
        poller._fetch_schedule()
        assert poller.get_outage_status()["status"] == "clear"

    def test_recent_schedule_survives_transient_failure(self):
        poller = OutageSchedulePoller(provider=self._OkProvider([(23, 0, 24, 0)]))
        poller._fetch_schedule()
        poller.provider = self._FailingProvider()
        poller._fetch_schedule()
        now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        poller._last_updated = now - timedelta(minutes=5)
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            status = poller.get_outage_status()
        assert status["status"] == "upcoming"

    def test_old_schedule_becomes_error(self):
        poller = OutageSchedulePoller(provider=self._FailingProvider())
        poller._fetch_schedule()
        now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        poller._windows = []
        poller._last_updated = now - SCHEDULE_MAX_AGE - timedelta(minutes=1)
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            status = poller.get_outage_status()
        assert status["status"] == "error"

    def test_yesterdays_schedule_is_not_used_today(self):
        poller = OutageSchedulePoller(provider=self._OkProvider([]))
        now = datetime.now().replace(hour=0, minute=5, second=0, microsecond=0)
        poller._last_updated = now - timedelta(minutes=10)  # 23:55 yesterday
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            status = poller.get_outage_status()
        assert status["status"] == "unknown"

    def test_after_midnight_the_last_fetchs_tomorrow_is_today(self):
        """A 00:00-04:00 outage published yesterday as "tomorrow" is active just
        after midnight, before the providers publish the new day as today."""
        poller = OutageSchedulePoller(provider=self._OkProvider([]))
        now = datetime.now().replace(hour=0, minute=5, second=0, microsecond=0)
        poller._windows = [(20, 0, 22, 0)]                     # yesterday's today
        poller._days = {now.date() - timedelta(days=1): [(20, 0, 22, 0)], now.date(): [(0, 0, 4, 0)]}
        poller._last_updated = now - timedelta(minutes=10)    # 23:55 yesterday
        with patch("outage_providers.base.datetime") as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.combine = datetime.combine
            mock_dt.min = datetime.min
            status = poller.get_outage_status()
        assert status["status"] == "active" and status["end_time"].hour == 4


class TestProvidersRaiseOnHttpError:
    @staticmethod
    def _response(status_code, json_data=None):
        resp = MagicMock()
        resp.ok = status_code < 400
        resp.status_code = status_code
        resp.text = "Error occurred"
        resp.json.return_value = json_data
        return resp

    def test_yasno_http_error(self):
        provider = YasnoProvider(group="4.2", region_id=26, dso_id=903)
        with patch("outage_providers.yasno.requests.get", return_value=self._response(500)):
            with pytest.raises(OutageFetchError, match="HTTP 500 for region 26 / DSO 903"):
                provider.fetch_windows()

    def test_yasno_missing_group(self):
        provider = YasnoProvider(group="4.2", region_id=25, dso_id=902)
        with patch("outage_providers.yasno.requests.get",
                   return_value=self._response(200, {"1.1": {}, "2.1": {}})):
            with pytest.raises(OutageFetchError, match="group '4.2' not found"):
                provider.fetch_windows()

    def test_lvivoblenergo_http_error(self):
        provider = LvivoblenergoProvider(group="4.1")
        with patch("outage_providers.lvivoblenergo.requests.get", return_value=self._response(502)):
            with pytest.raises(OutageFetchError, match="HTTP 502"):
                provider.fetch_windows()


class TestTomorrowFromOtherProviders:
    @staticmethod
    def _response(json_data):
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = json_data
        return resp

    def test_yasno_includes_tomorrow_with_slots(self):
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        data = {"4.2": {
            "today": {"slots": [{"start": 480, "end": 720, "type": "Definite"}],
                      "date": f"{today.isoformat()}T00:00:00+03:00"},
            "tomorrow": {"slots": [{"start": 1080, "end": 1320, "type": "Definite"},
                                   {"start": 0, "end": 60, "type": "NotPlanned"}],
                         "date": f"{tomorrow.isoformat()}T00:00:00+03:00"},
        }}
        provider = YasnoProvider(group="4.2")
        with patch("outage_providers.yasno.requests.get", return_value=self._response(data)):
            assert provider.fetch_days() == {today: [(8, 0, 12, 0)], tomorrow: [(18, 0, 22, 0)]}

    def test_yasno_omits_empty_tomorrow(self):
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        data = {"4.2": {
            "today": {"slots": [], "date": f"{today.isoformat()}T00:00:00+03:00"},
            "tomorrow": {"slots": [], "date": f"{tomorrow.isoformat()}T00:00:00+03:00"},
        }}
        provider = YasnoProvider(group="4.2")
        with patch("outage_providers.yasno.requests.get", return_value=self._response(data)):
            assert provider.fetch_days() == {today: []}

    def test_lvivoblenergo_includes_tomorrow(self):
        today = datetime.now().date()
        data = {"hydra:member": [{"menuItems": [
            {"name": "Today", "rawHtml": "<p>Група 4.1. з 09:00 до 12:00</p>"},
            {"name": "Tomorrow", "rawHtml": "<p>Група 4.1. з 18:00 до 21:00</p>"},
        ]}]}
        provider = LvivoblenergoProvider(group="4.1")
        with patch("outage_providers.lvivoblenergo.requests.get", return_value=self._response(data)):
            assert provider.fetch_days() == {
                today: [(9, 0, 12, 0)],
                today + timedelta(days=1): [(18, 0, 21, 0)],
            }

    def test_lvivoblenergo_omits_empty_tomorrow(self):
        today = datetime.now().date()
        data = {"hydra:member": [{"menuItems": [
            {"name": "Today", "rawHtml": ""},
            {"name": "Tomorrow", "rawHtml": ""},
        ]}]}
        provider = LvivoblenergoProvider(group="4.1")
        with patch("outage_providers.lvivoblenergo.requests.get", return_value=self._response(data)):
            assert provider.fetch_days() == {today: []}


class TestPollerDays:
    class _DaysProvider(OutageProvider):
        def __init__(self, days):
            self.days = days

        def fetch_days(self):
            return self.days

    def test_days_include_today_and_tomorrow_as_datetimes(self):
        today = datetime.now().date()
        tomorrow = today + timedelta(days=1)
        poller = OutageSchedulePoller(provider=self._DaysProvider({
            today: [],
            tomorrow: [(22, 0, 24, 0)],
        }))
        poller._fetch_schedule()
        status = poller.get_outage_status()
        assert status["status"] == "clear"
        assert status["days"] == [
            {"date": today, "windows": []},
            {"date": tomorrow, "windows": [(
                datetime.combine(tomorrow, datetime.min.time()).replace(hour=22),
                datetime.combine(tomorrow + timedelta(days=1), datetime.min.time()),
            )]},
        ]

    def test_days_omit_unpublished_tomorrow(self):
        today = datetime.now().date()
        poller = OutageSchedulePoller(provider=self._DaysProvider({today: []}))
        poller._fetch_schedule()
        assert [d["date"] for d in poller.get_outage_status()["days"]] == [today]

    def test_no_days_on_error(self):
        class Failing(OutageProvider):
            def fetch_days(self):
                raise OutageFetchError("down")
        poller = OutageSchedulePoller(provider=Failing())
        poller._fetch_schedule()
        assert "days" not in poller.get_outage_status()



class TestProvidersReportErrors:
    """A failure must never read as "no outages"."""

    def _loe(self, today_html):
        from unittest.mock import MagicMock
        resp = MagicMock(ok=True)
        resp.json.return_value = {"hydra:member": [{"menuItems": [
            {"name": "Today", "rawHtml": today_html}, {"name": "Tomorrow", "rawHtml": ""}]}]}
        return resp

    def test_loe_wrong_group_is_an_error(self):
        from outage_providers import OutageFetchError
        with patch("outage_providers.lvivoblenergo.requests.get",
                   return_value=self._loe("<p>Група 1.1. з 09:00 до 12:00</p>")):
            with pytest.raises(OutageFetchError, match="4.1"):
                LvivoblenergoProvider(group="4.1").fetch_days()

    def test_loe_without_any_groups_is_no_outages(self):
        with patch("outage_providers.lvivoblenergo.requests.get",
                   return_value=self._loe("<p>Відключень не заплановано</p>")):
            days = LvivoblenergoProvider(group="4.1").fetch_days()
        assert list(days.values()) == [[]]

    @pytest.mark.parametrize("module, make", [
        ("lvivoblenergo", lambda: LvivoblenergoProvider(group="4.1")),
        ("yasno", lambda: __import__("outage_providers.yasno", fromlist=["x"]).YasnoProvider(group="2.1")),
    ])
    def test_network_and_json_errors_are_fetch_errors(self, module, make):
        from unittest.mock import MagicMock
        from outage_providers import OutageFetchError
        with patch(f"outage_providers.{module}.requests.get", side_effect=requests.ConnectionError("down")):
            with pytest.raises(OutageFetchError, match="ConnectionError"):
                make().fetch_days()
        bad = MagicMock(ok=True)
        bad.json.side_effect = ValueError("not json")
        with patch(f"outage_providers.{module}.requests.get", return_value=bad):
            with pytest.raises(OutageFetchError, match="JSON"):
                make().fetch_days()

    def test_yasno_yesterday_as_today_is_not_used(self):
        from datetime import date, timedelta
        from unittest.mock import MagicMock
        from outage_providers import OutageFetchError
        from outage_providers.yasno import YasnoProvider
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        resp = MagicMock(ok=True)
        resp.json.return_value = {"2.1": {"today": {"date": yesterday + "T00:00:00+02:00", "slots": []},
                                          "tomorrow": {}}}
        with patch("outage_providers.yasno.requests.get", return_value=resp):
            with pytest.raises(OutageFetchError, match="isn't published yet"):
                YasnoProvider(group="2.1").fetch_days()

    def test_yasno_this_day_still_sent_as_tomorrow_is_used(self):
        from datetime import date, timedelta
        from unittest.mock import MagicMock
        from outage_providers.yasno import YasnoProvider
        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        resp = MagicMock(ok=True)
        resp.json.return_value = {"2.1": {
            "today": {"date": yesterday + "T00:00:00+02:00", "slots": []},
            "tomorrow": {"date": today.isoformat() + "T00:00:00+02:00",
                         "slots": [{"start": 0, "end": 240, "type": "Definite"}]}}}
        with patch("outage_providers.yasno.requests.get", return_value=resp):
            assert YasnoProvider(group="2.1").fetch_days() == {today: [(0, 0, 4, 0)]}

    def test_poller_keeps_the_last_schedule_when_today_is_missing(self):
        from datetime import date, timedelta
        from unittest.mock import MagicMock
        from outage_providers import OutageSchedulePoller
        provider = MagicMock()
        today = date.today()
        provider.fetch_days.return_value = {today: [(9, 0, 12, 0)]}
        poller = OutageSchedulePoller(provider=provider)
        poller._fetch_schedule()
        provider.fetch_days.return_value = {today - timedelta(days=1): []}   # fetched before midnight
        poller._fetch_schedule()
        assert poller._days[today] == [(9, 0, 12, 0)]
