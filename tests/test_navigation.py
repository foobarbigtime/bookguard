"""Shared navigation and the redirects from page names before the UI redesign."""

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from app.routes.pages import router


PAGES = {
    "home.html": ("home", ""),
    "review.html": ("review", "books"),
    "index.html": ("review", "scan-results"),
    "triage.html": ("review", "triage"),
    "repairs.html": ("review", "repairs"),
    "activity.html": ("activity", ""),
    "history_detail.html": ("activity", ""),
    "system.html": ("system", "status"),
    "diagnostics.html": ("system", "advanced"),
    "settings.html": ("settings", ""),
}


def header(section, tab=""):
    env = Environment(loader=FileSystemLoader("templates"))
    module = env.get_template("_layout.html").module
    return str(module.site_header(section, tab, "0.6.0"))


def test_every_page_uses_the_shared_header_for_its_section():
    for name, (section, tab) in PAGES.items():
        source = Path("templates", name).read_text(encoding="utf-8")
        assert f'site_header("{section}", "{tab}", version)' in source, name
        assert 'class="topnav"' not in source, name


def test_header_has_four_sections_and_a_settings_gear():
    html = header("system")
    links = re.findall(r'class="nav-link[^"]*" href="([^"]+)"', html)
    assert links == ["/", "/review", "/activity", "/system"]
    assert 'href="/system" aria-current="page"' in html
    assert html.count('aria-current="page"') == 1
    assert 'href="/settings" aria-label="Settings"' in html
    assert "v0.6.0" in html


def test_settings_page_marks_only_the_gear_as_current():
    html = header("settings")
    assert html.count('aria-current="page"') == 1
    assert 'class="icon-link active" href="/settings"' in html


def test_older_review_pages_stay_reachable_as_tabs_until_replaced():
    review = header("review", "books")
    tabs = re.findall(r'<nav class="tabs".*?</nav>', review, re.S)[0]
    assert re.findall(r'href="([^"]+)"', tabs) == ["/review", "/review/triage", "/repairs", "/review/scan-results"]
    assert 'href="/review" class="active" aria-current="page"' in review
    assert 'class="subbar"' not in header("home")
    assert 'class="subbar"' not in header("activity")
    system = header("system", "advanced")
    assert 'href="/system/advanced" class="active" aria-current="page"' in system


def test_no_page_or_generated_link_uses_the_old_page_names():
    old = re.compile(r"""["'(]/(triage|history|diagnostics)(?=[/"'#?])""")
    for path in [*Path("templates").glob("*.html"), *Path("static").glob("*.js"), *Path("app").rglob("*.py")]:
        # Routers declare API paths relative to /api, and pages.py the redirects themselves;
        # Bindery's own API also has a /history endpoint.
        if path.parent.name == "routes" or path.name == "bindery_client.py":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "/api/" not in line:
                assert not old.search(line), f"{path}: {line.strip()}"


def test_old_page_urls_redirect_and_keep_their_query():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        expected = {
            "/triage": "/review/triage",
            "/triage?classification=REJECT&reason_code=MISMATCH": "/review/triage?classification=REJECT&reason_code=MISMATCH",
            "/attention": "/",
            "/?classification=MISSING": "/review/scan-results?classification=MISSING",
            "/history": "/activity",
            "/history/acquisition/5": "/activity/acquisition/5",
            "/diagnostics": "/system",
        }
        for old, new in expected.items():
            response = client.get(old, follow_redirects=False)
            assert response.status_code == 307, old
            assert response.headers["location"] == new
