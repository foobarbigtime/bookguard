from app.config import Settings, settings
from app.matcher import author_match, classify_audio, title_match


def _reset_settings():
    defaults = Settings()
    settings.__dict__.update(defaults.__dict__)


def setup_function():
    _reset_settings()


def test_title_match_multiword():
    assert title_match("The Girl Who Survived", "The Girl Who Survived 05-05")


def test_title_mismatch():
    assert not title_match("English Girl", "California Girl")


def test_author_match():
    assert author_match("James Patterson", "James Patterson / Michael Ledwidge")


def test_pen_name_alias_match_is_bidirectional():
    assert author_match("Stephen King", "Richard Bachman")
    assert author_match("Robert Galbraith", "J.K. Rowling")


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
