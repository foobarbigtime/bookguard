from fastapi.testclient import TestClient

import app.main as main
from app.request_origin import rejection_reason


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(main.settings, "scan_on_start", False)
    monkeypatch.setenv("BOOKGUARD_AUTH_USERNAME", "reader")
    monkeypatch.setenv("BOOKGUARD_AUTH_PASSWORD", "secret")
    return TestClient(main.app)


def test_cross_site_text_plain_cannot_change_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "allow_actions", False)
    monkeypatch.setattr(main.settings, "bindery_url", "http://bindery:8787")
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/settings",
            auth=("reader", "secret"),
            headers={
                "Content-Type": "text/plain",
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
            },
            content='{"allow_actions": true, "bindery_url": "https://evil.example"}',
        )
    assert response.status_code == 403
    assert main.settings.allow_actions is False
    assert main.settings.bindery_url == "http://bindery:8787"


def test_text_plain_body_rejected_even_without_origin_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "allow_actions", False)
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/settings",
            auth=("reader", "secret"),
            headers={"Content-Type": "text/plain"},
            content='{"allow_actions": true}',
        )
    assert response.status_code == 403
    assert main.settings.allow_actions is False


def test_form_encoded_confirmation_is_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/triage/1/detach",
            auth=("reader", "secret"),
            data={"confirm": "DETACH"},
        )
    assert response.status_code == 403


def test_same_origin_json_request_is_allowed(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/settings/reset",
            auth=("reader", "secret"),
            headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
            json={"confirm": "WRONG"},
        )
    # Passed the origin gate and reached the handler's confirmation check.
    assert response.status_code == 400


def test_unauthenticated_cross_site_request_still_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/settings", headers={"Sec-Fetch-Site": "cross-site"})
    # Safe methods are not origin-checked; auth still applies.
    assert response.status_code == 401


def test_rejection_rules():
    host = {"host": "bookguard.lan:8788"}
    assert rejection_reason({**host}) is None
    assert rejection_reason({**host, "content-type": "application/json"}) is None
    assert rejection_reason({**host, "content-type": "application/json; charset=utf-8"}) is None
    assert rejection_reason({**host, "sec-fetch-site": "same-site"}) is not None
    assert rejection_reason({**host, "sec-fetch-site": "none"}) is None
    assert rejection_reason({**host, "origin": "http://bookguard.lan:8788"}) is None
    assert rejection_reason({**host, "origin": "https://bookguard.lan:8788"}) is not None
    assert rejection_reason({**host, "origin": "http://bookguard.lan:9999"}) is not None
    assert rejection_reason({**host, "origin": "null"}) is not None
    assert rejection_reason({**host, "content-length": "5"}) is not None
    assert rejection_reason({**host, "content-type": "multipart/form-data"}) is not None


def test_trusted_origin_allows_reverse_proxy(monkeypatch):
    headers = {"host": "bookguard:8788", "origin": "https://books.example.com"}
    assert rejection_reason(headers) is not None
    monkeypatch.setenv("BOOKGUARD_TRUSTED_ORIGINS", "https://books.example.com/")
    assert rejection_reason(headers) is None
