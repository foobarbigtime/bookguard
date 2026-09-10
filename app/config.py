from __future__ import annotations

from dataclasses import dataclass, field
import os


DEFAULT_MUSIC_GENRES = [
    "rock", "classic rock", "pop", "country", "jazz", "metal", "heavy metal",
    "punk", "punk rock", "progressive rock", "alternative", "indie", "indie rock",
    "blues", "classical", "soundtrack", "film soundtrack", "movie soundtrack",
    "electronic", "electronica", "dance", "house", "techno", "trance", "ambient",
    "rap", "hip hop", "hip-hop", "r&b", "soul", "funk", "reggae", "folk",
    "christian & gospel",
]

DEFAULT_AUTHOR_ALIASES = [
    "J.K. Rowling = Robert Galbraith | Joanne Rowling | Joanne K. Rowling",
    "Stephen King = Richard Bachman",
    "Lemony Snicket = Daniel Handler",
]

REPAIR_MODES = {"off", "preview", "safe"}


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def _repair_mode(value: object, default: str = "preview") -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in REPAIR_MODES else default


def _lines_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    raw = raw.replace("\\n", "\n")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _clean_lines(raw: object) -> list[str]:
    if isinstance(raw, str):
        parts = raw.replace("\r", "\n").split("\n")
    elif isinstance(raw, list):
        parts = raw
    else:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        value = str(part).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            cleaned.append(value)
    return cleaned


@dataclass
class Settings:
    # Docker/deployment location for BookGuard's own persistent database.
    config_dir: str = os.getenv("CONFIG_DIR", "/config")

    # Bindery connection and library mapping.
    bindery_db: str = os.getenv("BINDERY_DB", "/bindery/bindery.db")
    bindery_url: str = os.getenv("BINDERY_URL", "http://host.docker.internal:8787").rstrip("/")
    bindery_api_key: str = os.getenv("BINDERY_API_KEY", "")
    audiobook_root: str = os.getenv("AUDIOBOOK_ROOT", "/audiobooks")
    audiobook_bindery_prefix: str = os.getenv(
        "AUDIOBOOK_BINDERY_PREFIX", "/data/media/audiobooks"
    ).rstrip("/")
    ebook_root: str = os.getenv("EBOOK_ROOT", "/books")
    ebook_bindery_prefix: str = os.getenv(
        "EBOOK_BINDERY_PREFIX", "/data/media/books"
    ).rstrip("/")
    quarantine_root: str = os.getenv("QUARANTINE_ROOT", "/quarantine")

    # Safety and scan behavior.
    allow_actions: bool = _bool("BOOKGUARD_ALLOW_ACTIONS", False)
    sample_files: int = _clamp(_int("BOOKGUARD_SAMPLE_FILES", 3), 1, 50)
    scan_on_start: bool = _bool("BOOKGUARD_SCAN_ON_START", False)
    scan_audiobooks: bool = _bool("BOOKGUARD_SCAN_AUDIOBOOKS", True)
    scan_ebooks: bool = _bool("BOOKGUARD_SCAN_EBOOKS", True)
    progress_every: int = _clamp(_int("BOOKGUARD_PROGRESS_EVERY", 10), 1, 100)

    # Matching behavior.
    title_min_shared_words: int = _clamp(_int("BOOKGUARD_TITLE_MIN_SHARED_WORDS", 2), 1, 5)
    allow_author_surname_match: bool = _bool("BOOKGUARD_AUTHOR_SURNAME_MATCH", True)
    reject_music_mismatch: bool = _bool("BOOKGUARD_REJECT_MUSIC_MISMATCH", True)
    reject_strong_mismatch: bool = _bool("BOOKGUARD_REJECT_STRONG_MISMATCH", True)
    strong_mismatch_min_samples: int = _clamp(
        _int("BOOKGUARD_STRONG_MISMATCH_MIN_SAMPLES", 1), 1, 10
    )
    strong_mismatch_consensus_percent: int = _clamp(
        _int("BOOKGUARD_STRONG_MISMATCH_CONSENSUS_PERCENT", 67), 50, 100
    )
    music_genres: list[str] = field(default_factory=lambda: list(DEFAULT_MUSIC_GENRES))
    author_aliases: list[str] = field(
        default_factory=lambda: _lines_env("BOOKGUARD_AUTHOR_ALIASES", DEFAULT_AUTHOR_ALIASES)
    )

    # Content verification. Native verification is core; Tika is an optional fallback.
    verification_enabled: bool = _bool("BOOKGUARD_VERIFICATION_ENABLED", True)
    verification_max_text_chars: int = _clamp(
        _int("BOOKGUARD_VERIFICATION_MAX_TEXT_CHARS", 1500000), 50000, 5000000
    )
    verification_pdf_pages: int = _clamp(
        _int("BOOKGUARD_VERIFICATION_PDF_PAGES", 24), 1, 100
    )
    verification_use_tika: bool = _bool("BOOKGUARD_VERIFICATION_USE_TIKA", True)
    verification_tika_url: str = os.getenv("BOOKGUARD_TIKA_URL", "").rstrip("/")

    # Metadata repair. Preview is intentionally the default; it never writes files.
    metadata_repair_mode: str = field(
        default_factory=lambda: _repair_mode(os.getenv("BOOKGUARD_METADATA_REPAIR_MODE", "preview"))
    )
    repair_audiobooks: bool = _bool("BOOKGUARD_REPAIR_AUDIOBOOKS", True)
    repair_ebooks: bool = _bool("BOOKGUARD_REPAIR_EBOOKS", True)
    repair_normalize_pass: bool = _bool("BOOKGUARD_REPAIR_NORMALIZE_PASS", True)
    repair_audio_album: bool = _bool("BOOKGUARD_REPAIR_AUDIO_ALBUM", True)
    repair_audio_album_artist: bool = _bool("BOOKGUARD_REPAIR_AUDIO_ALBUM_ARTIST", True)
    repair_audio_genre: bool = _bool("BOOKGUARD_REPAIR_AUDIO_GENRE", False)
    repair_audio_genre_value: str = os.getenv("BOOKGUARD_REPAIR_AUDIO_GENRE_VALUE", "Audiobook")

    # Dashboard behavior.
    dashboard_poll_ms: int = _clamp(_int("BOOKGUARD_DASHBOARD_POLL_MS", 1500), 500, 10000)
    live_results_limit: int = _clamp(_int("BOOKGUARD_LIVE_RESULTS_LIMIT", 20), 5, 100)
    dashboard_result_limit: int = _clamp(_int("BOOKGUARD_RESULT_LIMIT", 1000), 50, 5000)

    def apply(self, values: dict) -> None:
        string_fields = {
            "bindery_db", "bindery_url", "bindery_api_key", "audiobook_root",
            "audiobook_bindery_prefix", "ebook_root", "ebook_bindery_prefix",
            "quarantine_root", "repair_audio_genre_value", "verification_tika_url",
        }
        bool_fields = {
            "allow_actions", "scan_on_start", "scan_audiobooks", "scan_ebooks",
            "allow_author_surname_match", "reject_music_mismatch", "reject_strong_mismatch",
            "verification_enabled", "verification_use_tika",
            "repair_audiobooks", "repair_ebooks", "repair_normalize_pass",
            "repair_audio_album", "repair_audio_album_artist", "repair_audio_genre",
        }
        int_bounds = {
            "sample_files": (1, 50),
            "progress_every": (1, 100),
            "title_min_shared_words": (1, 5),
            "strong_mismatch_min_samples": (1, 10),
            "strong_mismatch_consensus_percent": (50, 100),
            "verification_max_text_chars": (50000, 5000000),
            "verification_pdf_pages": (1, 100),
            "dashboard_poll_ms": (500, 10000),
            "live_results_limit": (5, 100),
            "dashboard_result_limit": (50, 5000),
        }

        for key in string_fields:
            if key in values and values[key] is not None:
                value = str(values[key]).strip()
                if key in {
                    "bindery_url", "audiobook_bindery_prefix", "ebook_bindery_prefix",
                    "verification_tika_url",
                }:
                    value = value.rstrip("/")
                setattr(self, key, value)

        if "metadata_repair_mode" in values:
            self.metadata_repair_mode = _repair_mode(values["metadata_repair_mode"], self.metadata_repair_mode)

        for key in bool_fields:
            if key in values:
                value = values[key]
                if isinstance(value, str):
                    value = value.strip().lower() in {"1", "true", "yes", "on"}
                setattr(self, key, bool(value))

        for key, (minimum, maximum) in int_bounds.items():
            if key in values:
                try:
                    value = int(values[key])
                except (TypeError, ValueError):
                    continue
                setattr(self, key, _clamp(value, minimum, maximum))

        if "music_genres" in values:
            raw = values["music_genres"]
            if isinstance(raw, str):
                raw = raw.replace(",", "\n")
            self.music_genres = _clean_lines(raw)

        if "author_aliases" in values:
            self.author_aliases = _clean_lines(values["author_aliases"])

    def public_dict(self) -> dict:
        return {
            "bindery_db": self.bindery_db,
            "bindery_url": self.bindery_url,
            "bindery_api_key_set": bool(self.bindery_api_key),
            "audiobook_root": self.audiobook_root,
            "audiobook_bindery_prefix": self.audiobook_bindery_prefix,
            "ebook_root": self.ebook_root,
            "ebook_bindery_prefix": self.ebook_bindery_prefix,
            "quarantine_root": self.quarantine_root,
            "allow_actions": self.allow_actions,
            "sample_files": self.sample_files,
            "scan_on_start": self.scan_on_start,
            "scan_audiobooks": self.scan_audiobooks,
            "scan_ebooks": self.scan_ebooks,
            "progress_every": self.progress_every,
            "title_min_shared_words": self.title_min_shared_words,
            "allow_author_surname_match": self.allow_author_surname_match,
            "reject_music_mismatch": self.reject_music_mismatch,
            "reject_strong_mismatch": self.reject_strong_mismatch,
            "strong_mismatch_min_samples": self.strong_mismatch_min_samples,
            "strong_mismatch_consensus_percent": self.strong_mismatch_consensus_percent,
            "music_genres": list(self.music_genres),
            "author_aliases": list(self.author_aliases),
            "verification_enabled": self.verification_enabled,
            "verification_max_text_chars": self.verification_max_text_chars,
            "verification_pdf_pages": self.verification_pdf_pages,
            "verification_use_tika": self.verification_use_tika,
            "verification_tika_url": self.verification_tika_url,
            "metadata_repair_mode": self.metadata_repair_mode,
            "repair_audiobooks": self.repair_audiobooks,
            "repair_ebooks": self.repair_ebooks,
            "repair_normalize_pass": self.repair_normalize_pass,
            "repair_audio_album": self.repair_audio_album,
            "repair_audio_album_artist": self.repair_audio_album_artist,
            "repair_audio_genre": self.repair_audio_genre,
            "repair_audio_genre_value": self.repair_audio_genre_value,
            "dashboard_poll_ms": self.dashboard_poll_ms,
            "live_results_limit": self.live_results_limit,
            "dashboard_result_limit": self.dashboard_result_limit,
        }

    def persistable_dict(self) -> dict:
        data = self.public_dict()
        data.pop("bindery_api_key_set", None)
        data["bindery_api_key"] = self.bindery_api_key
        return data


settings = Settings()
