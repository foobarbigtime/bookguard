"""Wrong PASS results observed in the October 6 library audit."""

import pytest

from app.config import settings
from app.matcher import classify_audio, classify_ebook, title_match


@pytest.mark.parametrize("minimum", [1, 2, 5])
@pytest.mark.parametrize(
    ("expected", "observed"),
    [
        ("NYPD Red 5", "NYPD Red 2"),
        ("NYPD Red 3", "NYPD Red 4"),
        ("NYPD Red, Book 5", "NYPD Red, Book 2"),
        ("NYPD Red 5", "NYPD Red 2 - Part 5"),
        ("NYPD Red, Book 5", "NYPD Red, Book 2 - CD 5"),
        ("All-American Expedition", "All-American Murder"),
        ("All-American Expedition", "All-American Murder - The Rise and Fall of Aaron Hernandez"),
    ],
)
def test_shared_words_cannot_hide_a_different_work(monkeypatch, minimum, expected, observed):
    monkeypatch.setattr(settings, "title_min_shared_words", minimum)
    assert not title_match(expected, observed)
    assert classify_audio(expected, "James Patterson", [
        {"album": observed, "artist": "James Patterson", "title": "Part 1"},
    ])[:3] == ("REVIEW", 40, "PARTIAL_MATCH")
    assert classify_ebook(expected, "James Patterson", {
        "title": observed, "author": "James Patterson",
    })[0] == "REVIEW"


@pytest.mark.parametrize(
    ("expected", "samples"),
    [
        ("Cross Justice: (Alex Cross 23)", [
            {"album": "Cross Justice", "artist": "James Patterson"},
            {"album": "Ricket", "artist": "Mark Wayne McGinnis"},
            {"album": "Ricket", "artist": "Mark Wayne McGinnis"},
        ]),
        ("NYPD Red 3", [
            {"album": "NYPD Red 4", "artist": "James Patterson, Marshall Karp"},
            {"album": "NYPD Red 4", "artist": "James Patterson, Marshall Karp"},
            {"album": "Coffin Road", "artist": "Peter May"},
        ]),
    ],
)
def test_audited_mixed_folders_require_review(expected, samples):
    assert classify_audio(expected, "James Patterson", samples)[:3] == (
        "REVIEW", 80, "MIXED_AUDIO_CONTENT",
    )


def test_one_conflicting_file_blocks_pass_even_with_a_matching_track_title():
    assert classify_audio("Cross Justice", "James Patterson", [
        {"album": "Cross Justice", "artist": "James Patterson"},
        {"album": "Ricket", "title": "Cross Justice", "artist": "Mark Wayne McGinnis"},
    ])[:3] == ("REVIEW", 65, "CONFLICTING_AUDIO_IDENTITY")


def test_title_and_author_from_different_files_cannot_be_combined():
    assert classify_audio("Cross Justice", "James Patterson", [
        {"album": "Cross Justice", "artist": ""},
        {"album": "", "artist": "James Patterson", "title": "Chapter 2"},
    ])[:3] == ("REVIEW", 65, "CONFLICTING_AUDIO_IDENTITY")


@pytest.mark.parametrize(
    ("expected", "observed"),
    [
        ("NYPD Red 5", "NYPD Red 5"),
        ("NYPD Red 5", "NYPD Red 5 - Chapter 2"),
        ("Cross Justice: (Alex Cross 23)", "Cross Justice"),
        ("Mary, Mary: Alex Cross, Book 11", "Mary, Mary"),
        ("All-American Murder", "All-American Murder - The Rise and Fall of Aaron Hernandez"),
        ("Messenger, The", "The Messenger"),
    ],
)
def test_correct_titles_and_track_numbers_still_pass(expected, observed):
    assert classify_audio(expected, "James Patterson", [
        {"album": observed, "artist": "James Patterson", "title": "Chapter 01"},
    ])[0] == "PASS"


def test_explicit_collection_members_still_pass():
    assert classify_audio(
        "Gabriel Allon Series: Books 5-7: Prince of Fire / The Messenger / The Secret Servant",
        "Daniel Silva",
        [{"album": title, "artist": "Daniel Silva"} for title in (
            "Prince of Fire", "The Messenger", "The Secret Servant",
        )],
    )[0] == "PASS"


def test_shorter_different_work_is_not_a_collection_member():
    assert classify_audio("Cross Justice", "James Patterson", [
        {"album": "Cross", "artist": "James Patterson"},
    ])[0] == "REVIEW"


def test_unknown_or_generic_metadata_does_not_invent_a_conflict():
    assert classify_audio("Cross Justice", "James Patterson", [
        {"album": "Cross Justice", "artist": "James Patterson"},
        {"album": "CD 2", "title": "Track 01", "artist": ""},
        {"album": "Ricket", "probe_error": "Unreadable"},
    ])[0] == "PASS"


def test_scan_checks_conflict_outside_display_samples(tmp_path, monkeypatch):
    from app import scanner

    monkeypatch.setattr(settings, "sample_files", 3)
    probes = [
        {"path": f"{tmp_path}/{i}.mp3", "album": "Cross Justice", "artist": "James Patterson"}
        for i in range(5)
    ]
    probes[1]["album"] = "Ricket"
    monkeypatch.setattr(scanner, "verify_audiobook", lambda *args, **kwargs: {"files": probes})
    result = scanner._scan_one({
        "format": "audiobook", "stored_path": str(tmp_path),
        "title": "Cross Justice", "author": "James Patterson",
    })
    assert result["classification"] == "REVIEW"
    assert result["reason_code"] == "CONFLICTING_AUDIO_IDENTITY"
    assert len(result["metadata"]["samples"]) == 3
    assert all(p["album"] == "Cross Justice" for p in result["metadata"]["samples"])
