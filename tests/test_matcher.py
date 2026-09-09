from app.matcher import author_match, classify_audio, title_match


def test_title_match_multiword():
    assert title_match("The Girl Who Survived", "The Girl Who Survived 05-05")


def test_title_mismatch():
    assert not title_match("English Girl", "California Girl")


def test_author_match():
    assert author_match("James Patterson", "James Patterson / Michael Ledwidge")


def test_music_mismatch_is_reject():
    state, score, _ = classify_audio(
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


def test_good_audiobook_is_pass():
    state, score, _ = classify_audio(
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
