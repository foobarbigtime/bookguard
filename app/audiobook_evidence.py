from __future__ import annotations

from pathlib import Path
from typing import Any

from .audiobook_verification import verify_audio_files
from .config import settings
from .matcher import classify_audio
from .media_discovery import AUDIO_EXTENSIONS, representative_items
from .media_evidence import inspect_media_path
from .metadata import audio_metadata_summary


def build_audiobook_evidence(result: dict[str, Any], path: str) -> dict[str, Any]:
    """Build read-only technical, media-kind, and identity evidence for one audiobook."""
    inventory = inspect_media_path(path)
    items = list(inventory.get("items") or [])
    detected_audio = [
        str(item.get("path") or "")
        for item in items
        if item.get("kind") == "audiobook"
    ]
    named_audio = [
        str(item.get("path") or "")
        for item in items
        if Path(str(item.get("path") or "")).suffix.lower() in AUDIO_EXTENSIONS
    ]
    audio_paths = sorted(
        {value for value in [*detected_audio, *named_audio] if value},
        key=str.casefold,
    )
    ebook_items = [item for item in items if item.get("kind") == "ebook"]
    unsafe_items = [item for item in items if item.get("kind") == "unsafe"]

    base_evidence = {
        "expected": {
            "title": str(result.get("title") or ""),
            "author": str(result.get("author") or ""),
            "mediaKind": "audiobook",
        },
        "actualMedia": {
            "candidateCount": int(inventory.get("candidateCount") or 0),
            "counts": dict(inventory.get("counts") or {}),
            "detected": [
                {
                    "path": str(item.get("path") or ""),
                    "kind": str(item.get("kind") or "unknown"),
                    "detectedFormat": str(item.get("detectedFormat") or ""),
                    "confidence": int(item.get("confidence") or 0),
                    "source": str(item.get("source") or ""),
                }
                for item in items[:100]
            ],
            "truncated": len(items) > 100,
        },
    }

    if unsafe_items:
        return {
            "verdict": "UNSAFE_FILE",
            "confidence": 100,
            "source": "media-kind",
            "evidence": {
                **base_evidence,
                "explanation": (
                    "One or more audiobook media candidates are unsafe filesystem "
                    "objects such as symbolic links."
                ),
                "reasonCode": "UNSAFE_MEDIA_OBJECT",
            },
        }

    if not audio_paths and ebook_items:
        formats = sorted(
            {
                str(item.get("detectedFormat") or "ebook")
                for item in ebook_items
            }
        )
        return {
            "verdict": "WRONG_MEDIA_TYPE",
            "confidence": 100,
            "source": "media-kind",
            "evidence": {
                **base_evidence,
                "observedMediaKind": "ebook",
                "observedFormats": formats,
                "reasonCode": "EXPECTED_AUDIOBOOK_FOUND_EBOOK",
                "explanation": (
                    "Bindery expects an audiobook, but the tracked media path contains "
                    "deterministically identified ebook/document bytes and no readable "
                    "audio container."
                ),
            },
        }

    if not audio_paths:
        return {
            "verdict": "INSUFFICIENT_EVIDENCE",
            "confidence": 0,
            "source": "media-kind",
            "evidence": {
                **base_evidence,
                "reasonCode": "NO_SUPPORTED_AUDIO_EVIDENCE",
                "explanation": (
                    "No supported readable audio container or deterministic ebook "
                    "media kind could be established at the tracked audiobook path."
                ),
            },
        }

    technical = verify_audio_files(audio_paths)
    probes = list(technical.get("files") or [])
    samples = representative_items(
        probes,
        max(3, int(settings.sample_files)),
    )
    identity_classification, risk_score, reason_code, reasons = classify_audio(
        str(result.get("title") or ""),
        str(result.get("author") or ""),
        samples,
    )

    technical_summary = {
        key: value
        for key, value in technical.items()
        if key != "files"
    }
    metadata_summary = audio_metadata_summary(samples)
    evidence = {
        **base_evidence,
        "technical": technical_summary,
        "identity": {
            "classification": identity_classification,
            "riskScore": int(risk_score),
            "reasonCode": reason_code,
            "reasons": list(reasons),
            **metadata_summary,
            "sampleCount": len(samples),
        },
        "sampleFiles": [
            {
                "path": str(probe.get("path") or ""),
                "formatName": str(probe.get("format_name") or ""),
                "codec": str(probe.get("codec") or ""),
                "durationSeconds": probe.get("duration_seconds"),
                "title": str(probe.get("title") or ""),
                "album": str(probe.get("album") or ""),
                "author": str(probe.get("author") or ""),
                "artist": str(probe.get("artist") or ""),
                "albumArtist": str(probe.get("album_artist") or ""),
                "narrator": str(probe.get("narrator") or ""),
                "performer": str(probe.get("performer") or ""),
                "track": str(probe.get("track") or ""),
                "disc": str(probe.get("disc") or ""),
                "language": str(probe.get("language") or ""),
            }
            for probe in samples
        ],
    }

    if str(technical.get("verdict") or "") == "FAIL":
        evidence["reasonCode"] = str(
            technical.get("reason_code") or "AUDIO_INTEGRITY_FAILED"
        )
        evidence["explanation"] = (
            "One or more expected audiobook containers failed deterministic "
            "technical integrity checks."
        )
        return {
            "verdict": "UNSAFE_FILE",
            "confidence": 100,
            "source": "audiobook-technical",
            "evidence": evidence,
        }

    if identity_classification == "PASS":
        evidence["reasonCode"] = "AUDIOBOOK_IDENTITY_VERIFIED"
        evidence["explanation"] = (
            "Readable audiobook containers and embedded identity evidence support "
            "the expected Bindery title and author."
        )
        confidence = 95 if technical.get("verdict") == "PASS" else 90
        return {
            "verdict": "VERIFIED_CORRECT",
            "confidence": confidence,
            "source": "audiobook-evidence",
            "evidence": evidence,
        }

    if identity_classification == "REJECT":
        evidence["reasonCode"] = reason_code
        evidence["explanation"] = (
            "Readable audiobook metadata consistently supports a different identity "
            "or a non-audiobook media classification."
        )
        return {
            "verdict": "WRONG_CONTENT",
            "confidence": max(95, int(risk_score)),
            "source": "audiobook-evidence",
            "evidence": evidence,
        }

    evidence["reasonCode"] = reason_code
    evidence["explanation"] = (
        "The audiobook is technically readable, but available deterministic "
        "identity evidence is not strong enough to prove the Bindery assignment."
    )
    return {
        "verdict": "INSUFFICIENT_EVIDENCE",
        "confidence": max(0, min(89, 100 - int(risk_score))),
        "source": "audiobook-evidence",
        "evidence": evidence,
    }
