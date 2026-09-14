from app.bindery_client import BinderyClient, evaluate_replacement_candidate


def test_duplicate_wrong_release_from_second_indexer_is_rejected():
    result = {
        "approved": True,
        "guid": "https://nzbfinder.example/different-guid",
        "title": "Cat Hickey - Death of a Texan (epub)",
        "author": "",
        "bookTitle": "",
    }

    decision = evaluate_replacement_candidate(
        result,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )

    assert decision.safe is False
    assert "author" in decision.reason.lower()


def test_matching_title_and_author_is_allowed():
    result = {
        "approved": True,
        "title": "Aaron Blabey - Cat of Death! (retail) (epub)",
        "author": "",
        "bookTitle": "",
    }

    decision = evaluate_replacement_candidate(
        result,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )

    assert decision.safe is True


def test_bindery_rejection_is_preserved():
    result = {
        "approved": False,
        "rejection": "release is blocklisted",
        "title": "Aaron Blabey - Cat of Death! (epub)",
    }

    decision = evaluate_replacement_candidate(
        result,
        expected_title="Cat of Death!",
        expected_author="Aaron Blabey",
    )

    assert decision.safe is False
    assert decision.reason == "release is blocklisted"


def test_library_scan_uses_bindery_reconciliation_endpoint(monkeypatch):
    client = BinderyClient(base_url="http://bindery", api_key="test")
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path: calls.append((method, path)) or {"message": "started"},
    )

    result = client.scan_library()

    assert result == {"message": "started"}
    assert calls == [("POST", "/library/scan")]


def test_queue_listing_uses_complete_queue_endpoint(monkeypatch):
    client = BinderyClient(base_url="http://bindery", api_key="test")
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path: calls.append((method, path))
        or {"items": [], "partial": False},
    )

    result = client.list_queue()

    assert result == {"items": [], "partial": False}
    assert calls == [("GET", "/queue")]


def test_queue_removal_disables_client_and_file_deletion(monkeypatch):
    client = BinderyClient(base_url="http://bindery", api_key="test")
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, **kwargs: calls.append(
            (method, path, kwargs)
        ),
    )

    client.remove_queue_item(77)

    assert calls == [
        (
            "DELETE",
            "/queue/77",
            {
                "params": {
                    "removeFromClient": "false",
                    "deleteFiles": "false",
                }
            },
        )
    ]


def test_setting_update_uses_named_setting_endpoint(monkeypatch):
    client = BinderyClient(base_url="http://bindery", api_key="test")
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs)),
    )

    client.set_setting("import.mode", "external")

    assert calls == [
        (
            "PUT",
            "/setting/import.mode",
            {"json": {"value": "external"}},
        )
    ]


def test_manual_reassignment_preview_and_action_use_exact_path(monkeypatch):
    client = BinderyClient(base_url="http://bindery", api_key="test")
    calls = []
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, path, **kwargs: calls.append((method, path, kwargs))
        or {"status": "noop"},
    )

    tracked_path = "/data/media/books/Author/Book/Book.epub"
    preview = client.preview_manual_reassignment(tracked_path, 42)
    result = client.reassign_manual_import(tracked_path, 42)

    assert preview == {"status": "noop"}
    assert result == {"status": "noop"}
    assert calls == [
        (
            "GET",
            "/queue/manual-import/reassign/preview",
            {
                "params": {
                    "path": tracked_path,
                    "targetBookId": 42,
                    "format": "ebook",
                }
            },
        ),
        (
            "POST",
            "/queue/manual-import/reassign",
            {
                "json": {
                    "path": tracked_path,
                    "targetBookId": 42,
                    "format": "ebook",
                }
            },
        ),
    ]
