import pytest

from app.config import Settings, settings
from app.matcher import (
    analyze_audio_identity_set,
    author_match,
    catalogue_member_title_match,
    classify_audio,
    classify_ebook,
    title_match,
)


def _reset_settings():
    defaults = Settings()
    settings.__dict__.update(defaults.__dict__)


def setup_function():
    _reset_settings()


def test_title_match_multiword():
    assert title_match("The Girl Who Survived", "The Girl Who Survived 05-05")


def test_title_mismatch():
    assert not title_match("English Girl", "California Girl")


def test_catalogue_article_variants_match():
    assert title_match("Order", "The Order")
    assert title_match("Messenger, The", "The Messenger")
    assert title_match("Cellist, The", "The Cellist")


def test_author_match():
    assert author_match("James Patterson", "James Patterson / Michael Ledwidge")


def test_reversed_author_name_matches():
    assert author_match("Ann Patchett", "Patchett, Ann")


def test_pen_name_alias_match_is_bidirectional():
    assert author_match("Stephen King", "Richard Bachman")
    assert author_match("Robert Galbraith", "J.K. Rowling")


def test_swapped_ebook_metadata_is_pass():
    state, score, code, _ = classify_ebook(
        "Bel Canto",
        "Ann Patchett",
        {"title": "Patchett, Ann", "author": "Bel Canto"},
    )
    assert state == "PASS"
    assert score == 10
    assert code == "SWAPPED_METADATA"


def test_swapped_ebook_rule_is_strict():
    state, _, code, _ = classify_ebook(
        "Bel Canto",
        "Ann Patchett",
        {"title": "Patchett", "author": "Bel"},
    )
    assert state == "REVIEW"
    assert code == "MISMATCH"


def test_music_mismatch_is_reject():
    state, score, code, _ = classify_audio(
        "The Ghost Next Door",
        "R. L. Stine",
        [{
            "artist": "The Ghost Next Door",
            "album": "Classic Songs Of Death And Dismemberment",
            "title": "I Am The Monster",
            "genre": "Progressive Rock",
        }],
    )
    assert state == "REJECT"
    assert score == 100
    assert code == "MUSIC_MISMATCH"


def test_music_title_collision_is_still_reject():
    state, score, code, _ = classify_audio(
        "Something Borrowed",
        "Emily Giffin",
        [{
            "artist": "John Prine",
            "album": "Something Borrowed, Something New: A Tribute To John Anderson",
            "title": "Something Borrowed",
            "genre": "Country",
        }],
    )
    assert state == "REJECT"
    assert score == 100
    assert code == "MUSIC_MISMATCH"


def test_good_audiobook_is_pass():
    state, score, code, _ = classify_audio(
        "Finders Keepers",
        "Stephen King",
        [{
            "artist": "Stephen King",
            "album": "Finders Keepers",
            "title": "Chapter 01",
            "genre": "Audiobook",
        }],
    )
    assert state == "PASS"
    assert score < 20
    assert code == "MATCH"


def test_narrator_artist_with_author_in_album_is_pass():
    state, score, code, _ = classify_audio(
        "How to train your dragon",
        "Cressida Cowell",
        [{
            "artist": "David Tennant",
            "album": "Cressida Cowell - How To Train Your Dragon",
            "title": "Track 01",
            "genre": "Audiobook",
        }],
    )
    assert state == "PASS"
    assert score < 20
    assert code == "MATCH"


def test_wrong_spoken_word_is_strong_reject():
    state, score, code, reasons = classify_audio(
        "English Girl",
        "Daniel Silva",
        [{
            "artist": "T. Jefferson Parker",
            "album": "California Girl",
            "title": "California Girl 18-52",
            "genre": "Mystery",
        }],
    )
    assert state == "REJECT"
    assert score == 95
    assert code == "STRONG_MISMATCH"
    assert "California Girl" in reasons[0]


def test_composer_fallback_can_support_strong_mismatch_detection():
    state, score, code, reasons = classify_audio(
        "Something Blue",
        "Emily Giffin",
        [{
            "artist": "",
            "album_artist": "",
            "author": "",
            "composer": "Alan Drew",
            "album": "Gardens of Water",
            "title": "Chapter 01",
            "genre": "Fiction",
        }],
    )
    assert state == "REJECT"
    assert score == 95
    assert code == "STRONG_MISMATCH"
    assert "Gardens of Water" in reasons[0]


def test_blank_metadata_stays_review():
    state, _, code, _ = classify_audio(
        "11/22/63",
        "Stephen King",
        [{"artist": "", "album": "", "title": "", "genre": ""}],
    )
    assert state == "REVIEW"
    assert code == "MISMATCH"


def test_generic_track_title_does_not_trigger_strong_reject():
    state, _, code, _ = classify_audio(
        "Cut to the Chase",
        "Aaron Blabey",
        [{"artist": "Narrator Name", "album": "", "title": "Chapter 01", "genre": "Audiobook"}],
    )
    assert state == "REVIEW"
    assert code == "MISMATCH"


def test_richard_bachman_alias_blocks_false_strong_reject():
    state, _, code, _ = classify_audio(
        "Stephen King 1 3",
        "Stephen King",
        [{
            "artist": "Richard Bachman",
            "album": "The Regulators",
            "title": "Chapter 01",
            "genre": "Speech",
        }],
    )
    assert state == "REVIEW"
    assert code == "PARTIAL_MATCH"



def test_catalogue_member_title_accepts_explicit_box_set_member():
    expected = (
        "Daniel Silva - Gabriel Allon Series: Books 5-7: "
        "Prince of Fire / The Messenger / The Secret Servant"
    )
    assert catalogue_member_title_match(expected, "The Messenger")
    assert catalogue_member_title_match(expected, "Prince of Fire")
    assert not catalogue_member_title_match(expected, "The Defector")


def test_whole_set_detects_same_author_mixed_books():
    summary = analyze_audio_identity_set(
        "1st to Die",
        "James Patterson",
        [
            {"album": "WMC 01 - 1st to Die", "author": "James Patterson"},
            {"album": "WMC 01 - 1st to Die", "author": "James Patterson"},
            {"album": "WMC 02 - 2nd Chance", "author": "James Patterson"},
            {"album": "WMC 03 - 3rd Degree", "author": "James Patterson"},
        ],
    )

    assert summary["titleMatchCount"] == 2
    assert summary["titleMismatchCount"] == 2
    assert summary["distinctMismatchTitleCount"] == 2
    assert summary["mixedContent"] is True
    assert summary["hasEmbeddedContradiction"] is True


def test_whole_set_does_not_call_box_set_members_mixed():
    expected = (
        "Gabriel Allon Series: Books 5-7: "
        "Prince of Fire / The Messenger / The Secret Servant"
    )
    summary = analyze_audio_identity_set(
        expected,
        "Daniel Silva",
        [
            {"album": "Prince of Fire", "author": "Daniel Silva"},
            {"album": "The Messenger", "author": "Daniel Silva"},
            {"album": "The Secret Servant", "author": "Daniel Silva"},
        ],
    )

    assert summary["titleMismatchCount"] == 0
    assert summary["mixedContent"] is False


@pytest.mark.parametrize(
    ("expected", "sample"),
    [
        ("Cujo", {"album": "Cujo", "title": "Chapter 01", "artist": "Stephen King"}),
        ("Holly", {"album": "Holly", "title": "Holly 01", "artist": "Stephen King"}),
        ("It", {"album": "It", "title": "Track 3", "artist": "Stephen King"}),
        ("Messenger, The", {"album": "The Messenger", "title": "Part 2", "artist": "Daniel Silva"}),
        ("Cross", {"album": "Cross: Alex Cross, Book 12", "title": "01", "artist": "James Patterson"}),
        ("Order", {"album": "The Order: A Novel: Gabriel Al", "title": "Chapter 7", "artist": "Daniel Silva"}),
    ],
)
def test_short_audiobook_titles_match_their_album_tag(expected, sample):
    author = sample["artist"]
    classification, _, reason, _ = classify_audio(expected, author, [sample])
    assert (classification, reason) == ("PASS", "MATCH")


def test_a_different_album_with_generic_tracks_still_does_not_match():
    from app.matcher import audio_title_supported

    assert not audio_title_supported("Cujo", {"album": "Carrie", "title": "Chapter 01"})
    assert not audio_title_supported("Later", {"album": "", "title": "Chapter 2"})
    # A subtitle split only happens after a real subtitle marker.
    assert not audio_title_supported("Night", {"album": "Night: Shift and Other Stories"})


def test_whole_set_accepts_the_same_subtitle_form_as_the_samples():
    probes = [
        {"album": "Cross: Alex Cross, Book 12", "title": f"Chapter {n}", "artist": "James Patterson"}
        for n in range(1, 4)
    ]
    summary = analyze_audio_identity_set("Cross", "James Patterson", probes)
    assert summary["titleMismatchCount"] == 0
    assert summary["hasEmbeddedContradiction"] is False
    assert summary["mixedContent"] is False

    other = analyze_audio_identity_set("Cross", "James Patterson", [{"album": "Kiss the Girls: A Novel", "artist": "James Patterson"}])
    assert other["titleMismatchCount"] == 1
