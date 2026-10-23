"""Tests for the split-out static assets (static/css, static/js)."""
import re

import pytest
from tests.conftest import page_config


@pytest.fixture
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c, app_module


def _asset_urls(html):
    return re.findall(r'(?:href|src)="(/static/[^"]+)"', html)


def test_index_links_versioned_css_and_js(client):
    c, _ = client
    html = c.get("/").get_data(as_text=True)
    urls = _asset_urls(html)
    assert any(re.fullmatch(r"/static/css/dashboard\.css\?v=[0-9a-f]{12}", u) for u in urls), urls
    assert any(re.fullmatch(r"/static/js/dashboard\.js\?v=[0-9a-f]{12}", u) for u in urls), urls
    # No CSS or app JS left inline; the server's values come as JSON data
    assert "<style>" not in html
    assert "window.DASHBOARD_CONFIG" not in html
    assert set(page_config(html)) == {"title", "locales", "defaultLanguage", "firstRun", "alertsMapUrl",
                                      "alertsRegion", "alertsRegionCodes", "inverterTypes"}


def test_dashboard_title(client, monkeypatch):
    """Empty: the translated default in the app bar; set: shown as typed (escaped)."""
    c, _ = client
    monkeypatch.delenv("DASHBOARD_TITLE", raising=False)
    html = c.get("/").get_data(as_text=True)
    assert '<h1 data-i18n="app.title">' in html and page_config(html)["title"] == ""
    monkeypatch.setenv("DASHBOARD_TITLE", "Дача <Іванових>")
    html = c.get("/").get_data(as_text=True)
    assert "<h1>Дача &lt;Іванових&gt;</h1>" in html and page_config(html)["title"] == "Дача <Іванових>"


def test_assets_are_served(client):
    c, _ = client
    for url in _asset_urls(c.get("/").get_data(as_text=True)):
        resp = c.get(url)
        assert resp.status_code == 200, url
        assert len(resp.data) > 1000


def test_static_files_have_no_template_syntax(client):
    _, app_module = client
    import os
    for rel in ("css/dashboard.css", "js/dashboard.js"):
        with open(os.path.join(app_module.app.static_folder, rel), encoding="utf-8") as f:
            text = f.read()
        assert "{{" not in text and "{%" not in text, rel


def test_version_changes_with_content(client, tmp_path, monkeypatch):
    _, app_module = client
    (tmp_path / "x.css").write_text("a{}")
    monkeypatch.setattr(app_module.app, "static_folder", str(tmp_path))
    v1 = app_module.asset_version("x.css")
    assert app_module.asset_version("x.css") == v1  # cached
    (tmp_path / "x.css").write_text("b{}")
    import os
    st = os.stat(tmp_path / "x.css")
    os.utime(tmp_path / "x.css", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert app_module.asset_version("x.css") != v1
