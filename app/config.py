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
AUTOMATION_MODES = {"manual", "observe", "automatic"}
AUTOMATIC_ACTION_CODES = frozenset({
    "resolve_proven_associations",
    "quarantine_proven_foreign_media",
    "reacquire_missing_expected_media",
    "reconcile_final_state",
    "quarantine_proven_wrong_media",
    "reacquire_expected_media",
    "admit_and_reconcile",
    "correct_or_quarantine",
    "verify_and_reconcile",
    "quarantine_exact_media",
    "apply_metadata_repair",
    "resume_known_transition",
    "reconcile_known_admission",
    "correct_exact_registration_owner",
    "finish_guarded_cleanup",
    "request_alternate_grab",
    "reconcile_download_and_staging",
    "retry_grab_once",
    "reconcile_after_retry",
    "admit_verified_acquisition",
    "request_published_acquisition_scan",
    "prove_supported_no_replace_method",
    "retry_guarded_publication",
    "scan_and_reconcile_registration",
})
DEFAULT_MAX_STAGED_EBOOK_BYTES = 512 * 1024 * 1024
DEFAULT_ACQUISITION_COORDINATOR_INTERVAL_SECONDS = 10
DEFAULT_MALWARE_SCAN_TIMEOUT_SECONDS = 60
DEFAULT_MALWARE_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_VERIFICATION_SNAPSHOT_MAX_BYTES = 512 * 1024 * 1024


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class AuthSettings:
    username: str
    password: str


def load_auth_settings() -> AuthSettings:
    username = os.getenv("BOOKGUARD_AUTH_USERNAME", "bookguard").strip()
    password = os.getenv("BOOKGUARD_AUTH_PASSWORD", "")
    if not username:
        raise ConfigurationError("BOOKGUARD_AUTH_USERNAME must not be empty.")
    if ":" in username:
        raise ConfigurationError("BOOKGUARD_AUTH_USERNAME must not contain a colon.")
    if not password:
        raise ConfigurationError(
            "BOOKGUARD_AUTH_PASSWORD is required before BookGuard can serve protected routes."
        )
    return AuthSettings(username=username, password=password)


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


@dataclass(frozen=True)
class AutomationSettings:
    """Environment-only settings for the experimental maintenance workflow.

    These values are loaded on demand so tests and container restarts observe
    the current environment without mixing them into persisted UI settings.
    """

    automation_mode: str
    automatic_action_allowlist: tuple[str, ...]
    staging_root: str
    bindery_drop_folder: str
    automatic_reacquisition: bool
    acquisition_coordinator_enabled: bool
    acquisition_coordinator_interval_seconds: int
    max_staged_ebook_bytes: int
    ebook_actions_enabled: bool
    ebook_action_root: str
    admission_enabled: bool
    admission_root: str
    admission_bindery_root: str


def load_automation_settings() -> AutomationSettings:
    automation_mode = os.getenv("BOOKGUARD_AUTOMATION_MODE", "manual").strip().lower()
    if automation_mode not in AUTOMATION_MODES:
        raise ConfigurationError(
            "BOOKGUARD_AUTOMATION_MODE must be one of: manual, observe, automatic."
        )

    raw_allowlist = os.getenv("BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST", "")
    requested_actions = {
        item.strip()
        for item in raw_allowlist.replace("\n", ",").split(",")
        if item.strip()
    }
    unknown_actions = sorted(requested_actions - AUTOMATIC_ACTION_CODES)
    if unknown_actions:
        raise ConfigurationError(
            "BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST contains unsupported action code(s): "
            + ", ".join(unknown_actions)
        )
    automatic_action_allowlist = tuple(sorted(requested_actions))

    raw_limit = os.getenv("BOOKGUARD_MAX_STAGED_EBOOK_BYTES", "").strip()
    if not raw_limit:
        raw_limit = str(DEFAULT_MAX_STAGED_EBOOK_BYTES)
    try:
        max_staged_ebook_bytes = int(raw_limit)
    except ValueError as exc:
        raise ConfigurationError(
            "BOOKGUARD_MAX_STAGED_EBOOK_BYTES must be an integer."
        ) from exc
    if max_staged_ebook_bytes <= 0:
        raise ConfigurationError(
            "BOOKGUARD_MAX_STAGED_EBOOK_BYTES must be greater than zero."
        )

    return AutomationSettings(
        automation_mode=automation_mode,
        automatic_action_allowlist=automatic_action_allowlist,
        staging_root=os.getenv("BOOKGUARD_STAGING_ROOT", "/staging").strip() or "/staging",
        bindery_drop_folder=os.getenv("BOOKGUARD_BINDERY_DROP_FOLDER", "").strip(),
        automatic_reacquisition=_bool("BOOKGUARD_AUTOMATIC_REACQUISITION", False),
        acquisition_coordinator_enabled=_bool(
            "BOOKGUARD_ACQUISITION_COORDINATOR_ENABLED",
            False,
        ),
        acquisition_coordinator_interval_seconds=_clamp(
            _int(
                "BOOKGUARD_ACQUISITION_COORDINATOR_INTERVAL_SECONDS",
                DEFAULT_ACQUISITION_COORDINATOR_INTERVAL_SECONDS,
            ),
            2,
            300,
        ),
        max_staged_ebook_bytes=max_staged_ebook_bytes,
        ebook_actions_enabled=_bool("BOOKGUARD_EBOOK_ACTIONS_ENABLED", False),
        ebook_action_root=os.getenv(
            "BOOKGUARD_EBOOK_ACTION_ROOT",
            "/action-books",
        ).strip()
        or "/action-books",
        admission_enabled=_bool("BOOKGUARD_ADMISSION_ENABLED", False),
        admission_root=os.getenv("BOOKGUARD_ADMISSION_ROOT", "/admission-books").strip()
        or "/admission-books",
        admission_bindery_root=os.getenv(
            "BOOKGUARD_ADMISSION_BINDERY_ROOT",
            "/data/media/books",
        ).rstrip("/"),
    )


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
    verification_file_signatures: bool = _bool(
        "BOOKGUARD_VERIFICATION_FILE_SIGNATURES", True
    )
    verification_archive_safety: bool = _bool(
        "BOOKGUARD_VERIFICATION_ARCHIVE_SAFETY", True
    )
    verification_epub_structure: bool = _bool(
        "BOOKGUARD_VERIFICATION_EPUB_STRUCTURE", True
    )
    verification_pdf_integrity: bool = _bool(
        "BOOKGUARD_VERIFICATION_PDF_INTEGRITY", True
    )
    verification_malware_scan: bool = _bool(
        "BOOKGUARD_VERIFICATION_MALWARE_SCAN", False
    )
    # The scanner endpoint is deployment-only so the web UI cannot turn this
    # feature into an arbitrary network client.
    verification_clamd_host: str = os.getenv("BOOKGUARD_CLAMD_HOST", "").strip()
    verification_clamd_port: int = _clamp(_int("BOOKGUARD_CLAMD_PORT", 3310), 1, 65535)
    verification_malware_timeout_seconds: int = _clamp(
        _int(
            "BOOKGUARD_MALWARE_SCAN_TIMEOUT_SECONDS",
            DEFAULT_MALWARE_SCAN_TIMEOUT_SECONDS,
        ),
        1,
        300,
    )
    verification_malware_max_bytes: int = _clamp(
        _int("BOOKGUARD_MALWARE_MAX_BYTES", DEFAULT_MALWARE_MAX_BYTES),
        1024 * 1024,
        2 * 1024 * 1024 * 1024,
    )
    # Deployment-only bound for the private, disk-backed verification snapshot.
    verification_snapshot_max_bytes: int = _clamp(
        _int(
            "BOOKGUARD_VERIFICATION_SNAPSHOT_MAX_BYTES",
            DEFAULT_VERIFICATION_SNAPSHOT_MAX_BYTES,
        ),
        1024 * 1024,
        2 * 1024 * 1024 * 1024,
    )

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
            "quarantine_root", "repair_audio_genre_value",
        }
        bool_fields = {
            "allow_actions", "scan_on_start", "scan_audiobooks", "scan_ebooks",
            "allow_author_surname_match", "reject_music_mismatch", "reject_strong_mismatch",
            "verification_enabled", "verification_use_tika",
            "verification_file_signatures", "verification_archive_safety",
            "verification_epub_structure",
            "verification_pdf_integrity", "verification_malware_scan",
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
            "verification_file_signatures": self.verification_file_signatures,
            "verification_archive_safety": self.verification_archive_safety,
            "verification_epub_structure": self.verification_epub_structure,
            "verification_pdf_integrity": self.verification_pdf_integrity,
            "verification_malware_scan": self.verification_malware_scan,
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
        """Return UI settings that are safe to store in BookGuard's database."""
        data = self.public_dict()
        data.pop("bindery_api_key_set", None)
        data.pop("verification_tika_url", None)
        return data


settings = Settings()
