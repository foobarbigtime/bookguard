from fastapi.testclient import TestClient

import app.main as main


def test_lifespan_and_http_basic_authentication(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setattr(main.settings, "config_dir", str(config))
    monkeypatch.setattr(main.settings, "scan_on_start", False)
    monkeypatch.setenv("BOOKGUARD_AUTH_USERNAME", "reader")
    monkeypatch.setenv("BOOKGUARD_AUTH_PASSWORD", "correct horse battery staple")

    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200

        unauthenticated = client.get("/api/settings")
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["www-authenticate"].startswith("Basic ")

        assert client.get("/api/settings", auth=("reader", "wrong")).status_code == 401
        assert client.get(
            "/api/settings",
            auth=("reader", "correct horse battery staple"),
        ).status_code == 200
        assert (config / "bookguard.db").is_file()

        monkeypatch.delenv("BOOKGUARD_AUTH_PASSWORD")
        assert client.get("/api/settings", auth=("reader", "anything")).status_code == 503


def test_mutating_routes_require_explicit_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(main.settings, "config_dir", str(tmp_path / "config"))
    monkeypatch.setattr(main.settings, "scan_on_start", False)
    monkeypatch.setenv("BOOKGUARD_AUTH_USERNAME", "reader")
    monkeypatch.setenv("BOOKGUARD_AUTH_PASSWORD", "secret")

    with TestClient(main.app) as client:
        auth = ("reader", "secret")
        assert client.post("/api/scan", auth=auth, json={"confirm": "WRONG"}).status_code == 400
        assert client.post("/api/settings/reset", auth=auth, json={"confirm": "WRONG"}).status_code == 400
        assert client.post(
            "/api/results/1/repair",
            auth=auth,
            json={"confirm": "WRONG"},
        ).status_code == 400
