"""The Home summary: the five questions an operator asks on arrival.

Is BookGuard healthy? Is anything waiting for me? What is it doing right now?
Is automation on, and what may it do? What happened recently?

Read-only. Everything comes from BookGuard's own database and settings, plus
one short virus-scanner probe when a scanner is configured; Bindery's API is
not called, so Home stays fast when Bindery is slow or down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from typing import Any

from . import __version__
from .activity import activity_events
from .attention import attention_snapshot
from .config import ConfigurationError, load_automation_settings, settings
from .db import latest_counts, latest_scan, local_conn
from .diagnostics import _malware_report
from .health import health_checks
from .history import operation_history
from .scanner import scan_is_running
from .import_watch import watcher as import_watcher
from .library_review import GROUP_LABELS, group_counts, open_review_items
from .services.dashboard import scan_timing
from .verifier import verification_job_status

RECENT_DAYS = 7

# Workflow attention (interrupted or refused work), grouped by what it is.
_WORKFLOW_GROUPS: dict[str, tuple[str, str]] = {
    "acquisition": ("replacements", "Replacements are waiting for you"),
    "admission": ("replacements", "Replacements are waiting for you"),
    "coordinator": ("replacements", "Replacements are waiting for you"),
    "hardlink_correction": ("shared", "A shared-file correction was interrupted"),
    "hardlink_cleanup": ("shared", "A shared-file correction was interrupted"),
    "recovery_plan": ("refused", "Automatic steps were refused by a safety check"),
    "automatic_execution": ("interrupted", "Automatic steps were interrupted"),
    "observe": ("observe", "Observe check suggestions"),
}

_VERIFICATION_COPY: dict[str, dict[str, Any]] = {
    "unsafe": {
        "title": ("1 damaged or unsafe file", "{n} damaged or unsafe files"),
        "done": "checked each file's structure and, where enabled, scanned it for viruses.",
        "action": "Review these files",
    },
    "move": {
        "title": ("1 ebook is filed under the wrong book", "{n} ebooks are filed under the wrong book"),
        "done": "matched each file's ISBN and title to another book that has no ebook. Each can be moved there without a download.",
        "action": "Review the moves",
    },
    "duplicate": {
        "title": ("1 file is a copy of another book", "{n} files are copies of other books"),
        "done": "matched each file to another book that already has its own ebook.",
        "action": "Review the copies",
    },
    "wrong": {
        "title": ("1 book contains the wrong file", "{n} books contain the wrong file"),
        "done": "verified each file. They can be replaced with the right book.",
        "action": "Review the list",
    },
    "metadata": {
        "title": ("1 book has the right file but wrong details", "{n} books have the right file but wrong details"),
        "done": "confirmed each file is the right book. Only the title or author stored in the file is wrong.",
        "action": "Review the fixes",
    },
}


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _examples(items: list[dict], limit: int = 2) -> str:
    names = [
        f"{item['title']}" + (f" ({item['author']})" if item["author"] else "")
        for item in items[:limit]
    ]
    if not names:
        return ""
    more = len(items) - len(names)
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def _verification_groups(items: list[dict]) -> list[dict[str, Any]]:
    groups = []
    for key, copy in _VERIFICATION_COPY.items():
        members = [item for item in items if item["group"] == key]
        if not members:
            continue
        n = len(members)
        groups.append({
            "key": key,
            "tone": "red" if key == "unsafe" else "blue" if key == "metadata" else "amber",
            "count": n,
            "title": _plural(n, *copy["title"]).format(n=n),
            "why": f"For example {_examples(members)}.",
            "done": "BookGuard has " + copy["done"],
            "actions": [{"label": copy["action"], "href": f"/review?group={key}", "primary": True}],
            "items": [],
        })
    return groups


def _workflow_groups(attention: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in attention.get("items") or []:
        key, title = _WORKFLOW_GROUPS.get(str(item.get("kind")), ("other", "Other work needs attention"))
        group = grouped.setdefault(key, {
            "key": key,
            "tone": "grey" if key == "observe" else "amber",
            "count": 0,
            "title": title,
            "why": "",
            "done": "BookGuard has stopped this work safely and changed nothing further.",
            "actions": [],
            "items": [],
        })
        group["count"] += 1
        group["items"].append(item)
    for group in grouped.values():
        first = group["items"][0]
        group["why"] = str(first.get("message") or "")
        if group["key"] == "observe":
            group["done"] = "BookGuard recorded what it would do. Observe checks never change anything."
        href = str(first.get("href") or "")
        if href:
            group["actions"].append({"label": "Open", "href": href, "primary": True})
    return list(grouped.values())


def _missing_group() -> list[dict[str, Any]]:
    missing = int(latest_counts().get("MISSING") or 0)
    if not missing:
        return []
    return [{
        "key": "missing",
        "tone": "grey",
        "count": missing,
        "title": f"{missing} Bindery {_plural(missing, 'entry points', 'entries point')} to a missing file",
        "why": "Bindery tracks a file that is no longer on disk.",
        "done": "BookGuard has confirmed each file is missing. Nothing was changed.",
        "actions": [{"label": "Review missing files", "href": "/review/scan-results?classification=MISSING", "primary": False}],
        "items": [],
    }]


def _undecided_group(counts: dict[str, int], unverified: int) -> list[dict[str, Any]]:
    undecided = counts.get("undecided", 0)
    if not undecided:
        return []
    why = (
        f"{unverified} {_plural(unverified, 'has', 'have')} not been verified yet."
        if unverified
        else "Verification could not prove what these files are."
    )
    return [{
        "key": "undecided",
        "tone": "grey",
        "count": undecided,
        "title": f"{undecided} {_plural(undecided, 'book is', 'books are')} undecided",
        "why": why,
        "done": "BookGuard has flagged them during the library scan and changed nothing.",
        "actions": [{"label": "Review undecided books", "href": "/review?group=undecided", "primary": False}],
        "items": [],
    }]


def _health(scan: dict | None, malware: dict[str, Any], automation_error: str | None) -> dict[str, Any]:
    problems: list[dict[str, str]] = [
        {"level": check["level"], "text": check["title"], "href": "/system"}
        for check in health_checks(malware, deep=False)["problems"]
        if check["level"] in {"error", "warn"}
    ]
    if automation_error:
        problems.append({"level": "error", "text": f"Automation settings are invalid: {automation_error}", "href": "/system"})
    if scan and scan.get("status") == "failed":
        problems.append({"level": "error", "text": "The last library scan failed.", "href": "/review/scan-results"})
    if not scan:
        problems.append({"level": "warn", "text": "The library has not been scanned yet.", "href": ""})
    level = "error" if any(p["level"] == "error" for p in problems) else "warn" if problems else "ok"
    title = {
        "ok": "BookGuard is healthy",
        "warn": "BookGuard needs attention",
        "error": "BookGuard has a problem",
    }[level]
    return {"level": level, "title": title, "problems": problems}


def _now(scan: dict | None) -> dict[str, Any]:
    job = verification_job_status()
    # A scan left "running" by a restart is not running; Health reports it.
    if scan and scan.get("status") == "running" and scan_is_running():
        timing = scan_timing(scan)
        return {
            "active": True,
            "label": "Scanning the library",
            "processed": int(scan.get("processed") or 0),
            "total": int(scan.get("total") or 0),
            "percent": timing["percent"],
            "etaSeconds": timing["eta_seconds"],
            "current": "",
        }
    if job.get("status") == "running":
        total = int(job.get("total") or 0)
        processed = int(job.get("processed") or 0)
        return {
            "active": True,
            "label": "Checking library files",
            "processed": processed,
            "total": total,
            "percent": round(processed / total * 100.0, 1) if total else 0.0,
            "etaSeconds": None,
            "current": str(job.get("current") or ""),
        }
    return {"active": False, "label": "Nothing is running", "lastScan": scan}


def _last_observe() -> str:
    with local_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_observations'"
        ).fetchone()
        if not exists:
            return ""
        row = conn.execute("SELECT MAX(last_seen_at) AS at FROM automation_observations").fetchone()
    return str(row["at"] or "") if row else ""


_MODE_SENTENCES = {
    "manual": "BookGuard only changes things when you click. It does nothing on its own.",
    "observe": "BookGuard decides what it would do and records it, but changes nothing.",
    "automatic": "BookGuard carries out the allowed actions on its own, one at a time, after fresh safety checks.",
}


def _automation() -> tuple[dict[str, Any], dict[str, Any], str | None]:
    try:
        automation = load_automation_settings()
    except ConfigurationError as exc:
        return {"mode": "invalid", "sentence": str(exc), "allowed": 0, "lastObserve": _last_observe()}, {}, str(exc)
    return (
        {
            "mode": automation.automation_mode,
            "sentence": _MODE_SENTENCES[automation.automation_mode],
            "allowed": len(automation.automatic_action_allowlist),
            "lastObserve": _last_observe(),
        },
        {
            "replacements": bool(automation.automatic_reacquisition and settings.allow_actions),
            "coordinator": bool(automation.acquisition_coordinator_enabled),
        },
        None,
    )


def _import_line() -> dict[str, str]:
    status = import_watcher.status()
    if not status["enabled"]:
        return {"key": "imports", "label": "Import checks", "level": "off", "text": "Off"}
    if status["lastError"]:
        return {"key": "imports", "label": "Import checks", "level": "warn", "text": "Could not read Bindery's database"}
    every = f"every {status['minutes']} min"
    if status["lastCheckAt"]:
        return {"key": "imports", "label": "Import checks", "level": "ok", "text": f"On \u00b7 {every} \u00b7 last looked",
                "at": status["lastCheckAt"]}
    return {"key": "imports", "label": "Import checks", "level": "ok", "text": f"On \u00b7 {every}"}


def _system(malware: dict[str, Any], gates: dict[str, Any], scan: dict | None) -> list[dict[str, str]]:
    revision = os.getenv("BOOKGUARD_BUILD_REVISION", "").strip()
    if malware["configured"] and malware["reachable"]:
        scanner = ("ok", f"Running · {malware['version']}" if malware.get("version") else "Running")
    elif malware["configured"]:
        scanner = ("warn", "Not answering")
    else:
        scanner = ("off", "Not set up")
    bindery_ok = os.path.exists(settings.bindery_db)
    actions = "actions on" if settings.allow_actions else "audit only"
    last_scan = ""
    if scan and scan.get("status") == "complete":
        last_scan = str(scan.get("finished_at") or "")
    return [
        {"key": "bookguard", "label": "BookGuard", "level": "ok",
         "text": f"v{__version__}" + (f" · {revision[:7]}" if revision and revision != "unknown" else "")},
        {"key": "scanner", "label": "Virus scanner", "level": scanner[0], "text": scanner[1]},
        {"key": "bindery", "label": "Bindery", "level": "ok" if bindery_ok else "error",
         "text": f"Database readable · {actions}" if bindery_ok else "Database not found"},
        _import_line(),
        {"key": "replacements", "label": "Replacements", "level": "ok" if gates.get("replacements") else "off",
         "text": "Set up" if gates.get("replacements") else "Not set up"},
        {"key": "scan", "label": "Library scan", "level": "ok" if last_scan else "off",
         "text": "Last finished", "at": last_scan} if last_scan else
        {"key": "scan", "label": "Library scan", "level": "off", "text": "No finished scan yet"},
    ]


def _recent() -> dict[str, Any]:
    events = operation_history(500)["items"]
    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat()
    window = [event for event in events if str(event.get("timestamp") or "") >= since]
    totals = {
        "added": sum(1 for e in window if e["kind"] == "admission" and e["status"] == "registered"),
        "quarantined": sum(
            1 for e in window if e["kind"] == "cleanup" and e["kindLabel"] == "Quarantine" and e["status"] == "applied"
        ),
        "blocked": sum(1 for e in window if e["kind"] == "recovery_plan" and e["status"] == "blocked"),
    }
    return {
        "days": RECENT_DAYS,
        "totals": totals,
        # The same plain sentences as Activity; per-file checks are folded per day there.
        "events": activity_events(200)[:6],
    }


def _safely(load, fallback, failures: list[str], what: str):
    """Home must still load when one source cannot be read; say so instead."""
    try:
        return load()
    except Exception as exc:  # noqa: BLE001 - reported on Home, never hidden
        failures.append(f"BookGuard could not read {what}: {str(exc)[:200]}")
        return fallback


def home_summary() -> dict[str, Any]:
    scan = latest_scan()
    items = open_review_items()
    counts = group_counts(items)
    unverified = sum(1 for item in items if not item["verified"])
    automation, gates, automation_error = _automation()
    malware = _malware_report()
    failures: list[str] = []
    attention = (
        _verification_groups(items)
        + _workflow_groups(_safely(attention_snapshot, {"items": []}, failures, "the attention queue"))
        + _missing_group()
        + _undecided_group(counts, unverified)
    )
    recent = _safely(_recent, {"days": RECENT_DAYS, "totals": {"added": 0, "quarantined": 0, "blocked": 0}, "events": []},
                     failures, "the activity history")
    health = _health(scan, malware, automation_error)
    if failures:
        health["problems"].extend({"level": "error", "text": text, "href": "/system"} for text in failures)
        health["level"] = "error"
        health["title"] = "BookGuard has a problem"
    decisions = sum(1 for group in attention if group["tone"] != "grey")
    library_counts = latest_counts()
    return {
        "health": health,
        "decisions": decisions,
        "library": {
            "checked": len(items),
            "passed": int(library_counts.get("PASS") or 0),
            "verified": counts["verified"],
            "wrongFile": counts["move"] + counts["duplicate"] + counts["wrong"],
            "damaged": counts["unsafe"],
            "details": counts["metadata"],
            "undecided": counts["undecided"],
            "groups": {key: {"label": GROUP_LABELS[key], "count": n} for key, n in counts.items()},
        },
        "attention": attention,
        "now": _now(scan),
        "automation": automation,
        "system": _system(malware, gates, scan),
        "recent": recent,
    }
