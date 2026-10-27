"""Shared test fixtures."""
import json
import os
import re

import pytest
from unittest.mock import patch, MagicMock
from inverters import DeyeInverter


@pytest.fixture(autouse=True)
def _isolated_data_files(tmp_path, monkeypatch):
    """Every test writes data files to its own temp folder, never to data/ (the
    user's live config and stats live there on a dev machine)."""
    import app
    import telegram_bot
    for name in ("INVERTER_CACHE_FILE", "POWER_HISTORY_FILE", "PHASE_STATS_FILE",
                 "GRID_DAILY_LOG_FILE", "GENERATOR_LOG_FILE", "INVERTER_CONFIG_FILE"):
        monkeypatch.setattr(app, name, str(tmp_path / os.path.basename(getattr(app, name))))
    monkeypatch.setattr(telegram_bot, "BATTERY_REPORT_DIR", str(tmp_path / "battery_reports"))
    # In-memory tracking state starts fresh for each test
    monkeypatch.setattr(app, "_grid_counter", {"last": None, "day": None})


def page_config(html):
    """window.DASHBOARD_CONFIG as the page embeds it (JSON in #dashboard-config)."""
    match = re.search(r'<script type="application/json" id="dashboard-config">(.*?)</script>', html, re.S)
    assert match, "no #dashboard-config in the page"
    return json.loads(match.group(1))


@pytest.fixture
def mock_inverter():
    """Create a DeyeInverter with a mocked PySolarmanV5 connection."""
    with patch("inverters.deye.PySolarmanV5"):
        inv = DeyeInverter(ip="192.168.1.1", serial=123456)
        inv.inverter = MagicMock()
        return inv


def mock_read_register(register_values):
    """Return a side_effect function that maps register addresses to values.

    Usage:
        inverter.read_register = mock_read_register({587: 5200, 588: 75})
    """
    def read_register(addr):
        return register_values.get(addr, 0)
    return read_register
