from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import recording_groups
from app.routes import triage
from app.unmatched import Library


def _probe(path: str, album: str, author: str = "Example Author", track: str = "1", title: str = "Chapter 1", **extra):
    return {"path": path, "album": album, "author": author, "track": track, "title": title, "disc": "1", **extra}


def _library():
    return Library([
        {"id": 20, "title": "The Example Book", "author": "Example Author", "series": []},
        {"id": 21, "title": "Another Story", "author": "Example Author", "series": []},
    ])


def _item(**extra):
    return {"id": 7, "book_id": 20, "format": "audiobook", "title": "The Example Book",
            "author": "Example Author", "local_path": "/audiobooks/mixed", **extra}


def test_mixed_folder_splits_into_works_matched_to_the_catalogue():
    probes = [
        _probe("/a/1.mp3", "The Example Book", track="1"),
        _probe("/a/2.mp3", "The Example Book", track="2"),
        _probe("/a/3.mp3", "Another Story", track="1"),
        _probe("/a/4.mp3", "Unlisted Tale", author="Someone Else", track="1"),
        _probe("/a/5.mp3", "", author="", title="Track 5"),
        {"path": "/a/6.mp3", "probe_error": "bad"},
    ]
    preview = recording_groups.group_probes(_item(), probes, _library())
    assert preview["splitNeeded"] and preview["workCount"] == 3
    assert preview["applicable"] is False
    first, second, third = preview["groups"]
    assert first["assigned"] and first["fileCount"] == 2 and first["book"]["id"] == 20
    assert not second["assigned"] and second["book"]["id"] == 21
    assert third["status"] == "not_in_catalogue" and third["title"] == "Unlisted Tale"
    assert preview["unidentified"] == ["/a/5.mp3"] and preview["unreadable"] == ["/a/6.mp3"]


def test_single_work_folder_needs_no_split():
    probes = [_probe(f"/a/{n}.mp3", "The Example Book", track=str(n)) for n in (1, 2, 3)]
    preview = recording_groups.group_probes(_item(), probes, _library())
    assert preview["workCount"] == 1 and preview["splitNeeded"] is False


def test_subtitle_forms_of_one_work_share_a_group():
    probes = [_probe("/a/1.mp3", "The Example Book"),
              _probe("/a/2.mp3", "The Example Book: A Novel", track="2")]
    preview = recording_groups.group_probes(_item(), probes, _library())
    assert preview["workCount"] == 1


def test_preview_reads_files_without_changing_them(tmp_path, monkeypatch):
    folder = tmp_path / "mixed"
    folder.mkdir()
    files = [folder / "1.mp3", folder / "2.mp3"]
    for index, path in enumerate(files):
        path.write_bytes(b"audio%d" % index)
    before = {p.name: p.read_bytes() for p in files}
    albums = {str(files[0]): "The Example Book", str(files[1]): "Another Story"}
    monkeypatch.setattr(recording_groups, "discover_audio_files", lambda target: [str(p) for p in files])
    monkeypatch.setattr(recording_groups, "cached_or_probe_audio_file",
                        lambda path: (_probe(path, albums[path]), False))
    preview = recording_groups.preview_recording_groups(_item(local_path=str(folder)), library=_library())
    assert preview["splitNeeded"] and preview["resultId"] == 7 and "Preview only" in preview["note"]
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_preview_refuses_ebooks_empty_and_oversized_folders(monkeypatch):
    with pytest.raises(ValueError):
        recording_groups.preview_recording_groups(_item(format="ebook"), library=_library())
    monkeypatch.setattr(recording_groups, "discover_audio_files", lambda target: [])
    with pytest.raises(ValueError):
        recording_groups.preview_recording_groups(_item(), library=_library())
    monkeypatch.setattr(recording_groups, "discover_audio_files",
                        lambda target: [f"/a/{n}.mp3" for n in range(recording_groups.MAX_FILES + 1)])
    with pytest.raises(ValueError):
        recording_groups.preview_recording_groups(_item(), library=_library())


def test_route_is_read_only_and_reports_conflicts(monkeypatch):
    monkeypatch.setattr(triage, "result_by_id", lambda result_id: _item(classification="REVIEW"))
    monkeypatch.setattr(triage, "preview_recording_groups", lambda item: {"resultId": item["id"], "groups": []})
    app = FastAPI()
    app.include_router(triage.router)
    with TestClient(app) as client:
        assert client.get("/api/triage/7/recording-groups").json() == {"resultId": 7, "groups": []}
        assert client.post("/api/triage/7/recording-groups").status_code == 405

        def refuse(item):
            raise ValueError("No audio files were found at this path.")
        monkeypatch.setattr(triage, "preview_recording_groups", refuse)
        assert client.get("/api/triage/7/recording-groups").status_code == 409
        monkeypatch.setattr(triage, "result_by_id", lambda result_id: _item(classification="PASS"))
        assert client.get("/api/triage/7/recording-groups").status_code == 400
