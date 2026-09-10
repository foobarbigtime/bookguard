from __future__ import annotations

import re

from . import verifier as legacy
from . import verifier_v048_refined as refined

# Final v0.4.8 safety refinement. Invalidate prior v0.4.8 verifier caches while
# keeping the public application version at 0.4.8.
legacy.VERIFIER_VERSION = "5"

COLLECTION_TITLE_PATTERNS = (
    r"\bcomplete\s+(?:series|collection|works|novels|stories)\b",
    r"\bomnibus\b",
    r"\bbox(?:ed)?\s+set\b",
    r"\bcollection\b",
    r"\banthology\b",
)

_base_classify_identity = refined._classify_identity


def _looks_collection_like(title: str) -> bool:
    value = str(title or "").strip().lower()
    return bool(value) and any(re.search(pattern, value, flags=re.I) for pattern in COLLECTION_TITLE_PATTERNS)


def _classify_identity(
    result: dict,
    metadata: dict,
    text: str,
    identifiers: list[str],
    source: str,
    notes: list[str],
    front_text: str = "",
) -> tuple[str, int, dict]:
    verdict, confidence, evidence = _base_classify_identity(
        result, metadata, text, identifiers, source, notes, front_text
    )

    # A collection/omnibus can legitimately contain the expected book title and
    # author near its front matter. That does not prove the entire file should be
    # relabelled as the single component work, so never authorize an automatic
    # metadata rewrite from that evidence alone.
    if verdict == "METADATA_ERROR":
        embedded_title = str(evidence.get("embedded", {}).get("title") or "")
        if _looks_collection_like(embedded_title):
            evidence["explanation"] = (
                "The embedded title looks like a collection, omnibus, anthology, or boxed set; "
                "the expected title may be only one component, so automatic metadata repair is withheld."
            )
            evidence.setdefault("notes", []).append(
                "Automatic metadata repair withheld by the v0.4.8 collection/omnibus safety gate."
            )
            return "INSUFFICIENT_EVIDENCE", 70, evidence

    return verdict, confidence, evidence


# Make the extraction path and legacy background/repair machinery use the final
# classifier and cache generation.
refined.base._classify_identity = _classify_identity
legacy.verify_result = refined.base.verify_result

verify_result = refined.base.verify_result
init_verification_db = refined.init_verification_db
verification_for_result = refined.verification_for_result
verification_summary = refined.verification_summary
verification_job_status = refined.verification_job_status
start_verification_job = refined.start_verification_job
test_tika = refined.test_tika
verified_repair_preview = refined.verified_repair_preview
apply_verified_metadata_repair = refined.apply_verified_metadata_repair
