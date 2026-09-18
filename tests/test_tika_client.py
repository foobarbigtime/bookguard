from types import SimpleNamespace

import app.tika_client as tika_client
from app.config import settings


def test_tika_url_with_credentials_is_rejected_before_upload(tmp_path, monkeypatch):
    book = tmp_path / "Book.epub"
    book.write_bytes(b"book")
    monkeypatch.setattr(settings, "verification_use_tika", True)
    monkeypatch.setattr(
        settings,
        "verification_tika_url",
        "https://user:secret@tika.example:9998",
    )

    def unexpected_put(*args, **kwargs):
        raise AssertionError("Tika request must not be attempted for an unsafe URL.")

    monkeypatch.setattr(tika_client.requests, "put", unexpected_put)

    text, error = tika_client.extract_text(str(book))

    assert text == ""
    assert "must not contain credentials" in error


def test_tika_upload_never_follows_redirects(tmp_path, monkeypatch):
    book = tmp_path / "Book.epub"
    book.write_bytes(b"book")
    monkeypatch.setattr(settings, "verification_use_tika", True)
    monkeypatch.setattr(settings, "verification_tika_url", "http://tika:9998")

    captured = {}

    def fake_put(url, **kwargs):
        captured["url"] = url
        captured["allow_redirects"] = kwargs.get("allow_redirects")
        return SimpleNamespace(status_code=302, text="redirect")

    monkeypatch.setattr(tika_client.requests, "put", fake_put)

    text, error = tika_client.extract_text(str(book))

    assert captured == {
        "url": "http://tika:9998/tika",
        "allow_redirects": False,
    }
    assert text == ""
    assert error == "Tika returned HTTP 302."


def test_tika_connection_never_follows_redirects(monkeypatch):
    monkeypatch.setattr(settings, "verification_tika_url", "http://tika:9998")

    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["allow_redirects"] = kwargs.get("allow_redirects")
        return SimpleNamespace(status_code=200, text="Apache Tika 3")

    monkeypatch.setattr(tika_client.requests, "get", fake_get)

    result = tika_client.test_connection()

    assert captured == {
        "url": "http://tika:9998/version",
        "allow_redirects": False,
    }
    assert result["configured"] is True
    assert result["ok"] is True
