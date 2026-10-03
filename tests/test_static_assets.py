"""After an upgrade a browser must not pair new pages with an old stylesheet."""

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import static_assets
from app.static_assets import RevalidatedStaticFiles, static_url


def test_every_template_links_static_files_through_versioned_urls():
    for template in Path("templates").glob("*.html"):
        source = template.read_text(encoding="utf-8")
        assert '"/static/' not in source, template
        for name in re.findall(r"static_url\('([^']+)'\)", source):
            assert (Path("static") / name).is_file(), f"{template}: {name}"


def test_url_changes_when_the_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(static_assets, "STATIC_DIR", tmp_path)
    static_assets._digest.cache_clear()
    (tmp_path / "style.css").write_text("body { color: red; }")
    before = static_url("style.css")
    static_assets._digest.cache_clear()
    (tmp_path / "style.css").write_text("body { color: blue; }")
    after = static_url("style.css")
    static_assets._digest.cache_clear()
    assert before.startswith("/static/style.css?v=") and after.startswith("/static/style.css?v=")
    assert before != after
    assert static_url("missing.css") == "/static/missing.css"


def test_static_responses_tell_browsers_to_revalidate():
    app = FastAPI()
    app.mount("/static", RevalidatedStaticFiles(directory="static"), name="static")
    with TestClient(app) as client:
        response = client.get(static_url("style.css"))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-cache"
        assert "--accent" in response.text
