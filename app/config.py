from dataclasses import dataclass
import os


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


@dataclass(frozen=True)
class Settings:
    bindery_db: str = os.getenv("BINDERY_DB", "/bindery/bindery.db")
    bindery_url: str = os.getenv("BINDERY_URL", "http://bindery:8787").rstrip("/")
    bindery_api_key: str = os.getenv("BINDERY_API_KEY", "")
    audiobook_root: str = os.getenv("AUDIOBOOK_ROOT", "/audiobooks")
    audiobook_bindery_prefix: str = os.getenv(
        "AUDIOBOOK_BINDERY_PREFIX", "/data/media/audiobooks"
    ).rstrip("/")
    ebook_root: str = os.getenv("EBOOK_ROOT", "/books")
    ebook_bindery_prefix: str = os.getenv(
        "EBOOK_BINDERY_PREFIX", "/data/media/books"
    ).rstrip("/")
    config_dir: str = os.getenv("CONFIG_DIR", "/config")
    quarantine_root: str = os.getenv("QUARANTINE_ROOT", "/quarantine")
    allow_actions: bool = _bool("BOOKGUARD_ALLOW_ACTIONS", False)
    sample_files: int = max(1, min(_int("BOOKGUARD_SAMPLE_FILES", 3), 20))
    scan_on_start: bool = _bool("BOOKGUARD_SCAN_ON_START", False)


settings = Settings()
