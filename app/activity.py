"""One plain-language timeline of everything BookGuard and the operator did.

Each durable record becomes an event: a sentence, **who** did it (You,
BookGuard, Automatic, System), how it **ended** (Done, Failed, Interrupted,
Waiting for you, In progress), and a link to the unchanged technical detail.
Per-file verifications and Observe decisions are folded into one event per
day, so a library check reads as "Checked 412 library files" rather than
412 rows.

Read-only: nothing here changes state. The one action offered, Undo on an
applied metadata repair, goes through the existing guarded endpoint.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import PurePosixPath
from typing import Any, Iterable

from .db import local_conn
from .history import operation_history

WHO = {"you": "You", "bookguard": "BookGuard", "automatic": "Automatic", "system": "System"}
RESULTS = {
    "done": "Done",
    "failed": "Failed",
    "interrupted": "Interrupted",
    "waiting": "Waiting for you",
    "running": "In progress",
}
PROBLEM_RESULTS = {"failed", "interrupted", "waiting"}
WHAT = {
    "checks": "Library checks",
    "decisions": "Your decisions",
    "replacements": "Replacements",
    "automation": "Automation",
    "system": "Settings and updates",
}
PERIODS = {"1": 1, "7": 7, "30": 30}

_VERDICT_WORDS = {
    "VERIFIED_CORRECT": "verified",
    "WRONG_CONTENT": "wrong file",
    "WRONG_MEDIA_TYPE": "wrong kind of media",
    "UNSAFE_FILE": "damaged",
    "METADATA_ERROR": "wrong details",
    "INSUFFICIENT_EVIDENCE": "undecided",
}


def _comparable(timestamp: str) -> str:
    """Stored timestamps are ISO 8601, some without a zone; treat those as UTC."""
    value = str(timestamp or "").replace(" ", "T")
    return value if ("+" in value[10:] or value.endswith("Z")) else value + "+00:00"


# Most serious first, so a day's summary leads with what needs attention.
_VERDICT_ORDER = ("damaged", "wrong file", "wrong kind of media", "wrong details", "undecided", "verified")


def _book(title: str) -> str:
    return f"“{title}”" if title else "a book"


def _event(
    *,
    key: str,
    kind: str,
    what: str,
    who: str,
    result: str,
    timestamp: str,
    sentence: str,
    title: str = "",
    author: str = "",
    path: str = "",
    detail: str = "",
    detail_href: str = "",
    actions: list[dict] | None = None,
    items: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "kind": kind,
        "what": what,
        "whatLabel": WHAT[what],
        "who": who,
        "whoLabel": WHO[who],
        "result": result,
        "resultLabel": RESULTS[result],
        "timestamp": _comparable(timestamp) if timestamp else "",
        "sentence": sentence,
        "title": title,
        "author": author,
        "path": path,
        "detail": detail,
        "detailHref": detail_href,
        "actions": actions or [],
        "items": items or [],
    }


def _status_result(status: str, done: Iterable[str], failed: Iterable[str] = ("failed",),
                   waiting: Iterable[str] = (), running: Iterable[str] = ()) -> str:
    if status in done:
        return "done"
    if status in failed:
        return "failed"
    if status in waiting:
        return "waiting"
    if status in running:
        return "running"
    return "interrupted"


# ---- records already in the operation history -------------------------------

def _from_history(item: dict[str, Any]) -> dict[str, Any] | None:
    kind, status = item["kind"], item["status"]
    title, label = item.get("title") or "", item.get("kindLabel") or ""
    base = {
        "key": f"{kind}-{item['id']}",
        "kind": kind,
        "timestamp": item.get("timestamp") or "",
        "title": title,
        "author": item.get("author") or "",
        "path": item.get("path") or "",
        "detail": item.get("message") or "",
        "detail_href": item.get("detailHref") or "",
    }
    if kind == "triage":
        return _event(**base, what="decisions", who="you", result="done",
                      sentence=f"You marked {_book(title)} reviewed")
    if kind in {"cleanup", "repair"} and status in {"applied", "undone"}:
        base["detail"] = ""  # only the internal action name; errors are kept
    if kind == "cleanup":
        result = _status_result(status, done={"applied"}, waiting={"attention"})
        verb = {"Quarantine": "quarantined", "Detach": "detached from Bindery",
                "Put back": "put back"}.get(label, "cleaned up")
        if result == "done":
            sentence = f"You {verb} {_book(title)}" + (" (nothing was deleted)" if label == "Quarantine" else "")
        elif result == "waiting":
            sentence = f"You {verb} {_book(title)}; Bindery needs you to finish"
        else:
            sentence = f"{label} of {_book(title)} {RESULTS[result].lower()}"
        return _event(**base, what="decisions", who="you", result=result, sentence=sentence)
    if kind == "repair":
        if status == "applied":
            return _event(**base, what="decisions", who="you", result="done",
                          sentence=f"You fixed the details of {_book(title)}",
                          actions=[{"kind": "undo-repair", "id": item["id"], "label": "Undo"}])
        if status == "undone":
            return _event(**base, what="decisions", who="you", result="done",
                          sentence=f"The details fix for {_book(title)} was undone")
        return _event(**base, what="decisions", who="you", result=_status_result(status, done=()),
                      sentence=f"Fixing the details of {_book(title)} did not finish")
    if kind == "acquisition":
        result = _status_result(status, done={"finalized"}, waiting={"review_required", "cleanup_required"},
                                running={"preparing", "grab_requested", "queued", "downloading", "awaiting_staging",
                                         "staging_observed", "verified", "admitted", "finalizing"})
        sentence = {
            "done": f"The replacement download for {_book(title)} finished",
            "failed": f"The replacement download for {_book(title)} failed",
            "waiting": f"The replacement download for {_book(title)} needs you",
        }.get(result, f"Replacement download for {_book(title)}: {status.replace('_', ' ')}")
        return _event(**base, what="replacements", who="bookguard", result=result, sentence=sentence)
    if kind == "admission":
        result = _status_result(status, done={"registered"}, waiting={"registration_conflict", "registration_correcting"},
                                running={"verified", "published", "scan_requested"})
        sentence = {
            "done": f"{_book(title).capitalize()} was added to the library",
            "failed": f"Adding {_book(title)} to the library failed",
            "waiting": f"Adding {_book(title)} to the library needs you",
        }.get(result, f"Adding {_book(title)} to the library: {status.replace('_', ' ')}")
        return _event(**base, what="replacements", who="bookguard", result=result, sentence=sentence)
    if kind in {"hardlink_correction", "hardlink_cleanup"}:
        result = _status_result(status, done={"applied", "cancelled"})
        what_done = "shared-file correction" if kind == "hardlink_correction" else "staging-link cleanup"
        sentence = (f"You applied a {what_done}" if status == "applied"
                    else f"A {what_done} was cancelled with nothing changed" if status == "cancelled"
                    else f"A {what_done} {RESULTS[result].lower()}")
        return _event(**base, what="decisions", who="you", result=result, sentence=sentence)
    if kind == "recovery_plan":
        result = _status_result(status, done={"completed", "superseded"},
                                waiting={"blocked"}, running={"planned", "ready", "running", "retry_wait"})
        sentence = (f"An automatic step for {_book(title)} was refused by a safety check" if status == "blocked"
                    else f"Automatic recovery for {_book(title)}: {status.replace('_', ' ')}")
        return _event(**base, what="automation", who="automatic", result=result, sentence=sentence)
    return None


def _fold_by_day(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    days: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        days[str(item.get("timestamp") or "")[:10]].append(item)
    return days


def _verification_days(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for day, group in _fold_by_day(items).items():
        counts = Counter(_VERDICT_WORDS.get(str(i["status"]).upper(), "undecided") for i in group)
        order = list(_VERDICT_ORDER)
        found = ", ".join(f"{counts[word]} {word}" for word in sorted(counts, key=order.index))
        events.append(_event(
            key=f"verification-day-{day}", kind="verification", what="checks", who="bookguard", result="done",
            timestamp=max(str(i["timestamp"]) for i in group),
            sentence=f"Checked {len(group)} library {'file' if len(group) == 1 else 'files'}: {found}",
            items=[{"text": f"{i['title']} · {_VERDICT_WORDS.get(str(i['status']).upper(), 'undecided')}",
                    "href": i["detailHref"]} for i in group[:100]],
        ))
    return events


def _observe_days(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for day, group in _fold_by_day(items).items():
        counts = Counter(str(i["status"]).replace("_", " ") for i in group)
        events.append(_event(
            key=f"observe-day-{day}", kind="observe", what="automation", who="automatic", result="done",
            timestamp=max(str(i["timestamp"]) for i in group),
            sentence=f"Observe check looked at {len(group)} {'item' if len(group) == 1 else 'items'} and changed nothing",
            detail=", ".join(f"{n} {word}" for word, n in counts.most_common()),
            items=[{"text": f"{i['title'] or 'Item'} · {str(i['status']).replace('_', ' ')}", "href": i["detailHref"]}
                   for i in group[:100]],
        ))
    return events


# ---- records the operation history does not cover ---------------------------

def _rows(conn, table: str, sql: str, limit: int) -> list:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return conn.execute(sql, (limit,)).fetchall() if exists else []


def _scan_events(conn, limit: int) -> list[dict[str, Any]]:
    events = []
    for row in _rows(conn, "scans", "SELECT * FROM scans ORDER BY started_at DESC LIMIT ?", limit):
        status = str(row["status"] or "")
        result = _status_result(status, done={"complete"}, running={"running"})
        total, processed = int(row["total"] or 0), int(row["processed"] or 0)
        sentence = {
            "done": f"Library scan finished: {total} {'book' if total == 1 else 'books'} checked",
            "running": f"Library scan running: {processed} of {total} books",
            "failed": "Library scan failed",
        }.get(result, f"Library scan stopped after {processed} of {total} books")
        events.append(_event(
            key=f"scan-{row['id']}", kind="scan", what="checks", who="bookguard", result=result,
            timestamp=str(row["finished_at"] or row["started_at"] or ""), sentence=sentence,
            detail=str(row["error"] or ""),
        ))
    return events


def _move_events(conn, limit: int) -> list[dict[str, Any]]:
    events = []
    for row in _rows(conn, "catalogue_moves", "SELECT * FROM catalogue_moves ORDER BY id DESC LIMIT ?", limit):
        status = str(row["status"] or "")
        result = _status_result(status, done={"confirmed"}, failed={"request_failed", "fingerprint_mismatch"},
                                waiting={"pending"}, running={"requested"})
        target = str(row["target_title"] or "")
        name = PurePosixPath(str(row["source_path"] or "")).name
        sentence = {
            "done": f"You moved a misfiled ebook to {_book(target)}",
            "waiting": f"Moving a misfiled ebook to {_book(target)} is waiting for Bindery",
            "failed": f"Moving a misfiled ebook to {_book(target)} failed",
        }.get(result, f"Moving a misfiled ebook to {_book(target)}: {status.replace('_', ' ')}")
        events.append(_event(
            key=f"move-{row['id']}", kind="catalogue_move", what="decisions", who="you", result=result,
            timestamp=str(row["updated_at"] or row["created_at"] or ""), sentence=sentence, title=target,
            path=f"{row['source_path']} → {row['destination']}", detail=name,
        ))
    return events


def _execution_events(conn, limit: int) -> list[dict[str, Any]]:
    events = []
    sql = """SELECT e.*, p.title AS title, p.author AS author FROM automatic_executions e
             LEFT JOIN recovery_plans p ON p.id = e.plan_id ORDER BY e.id DESC LIMIT ?"""
    for row in _rows(conn, "automatic_executions", sql, limit):
        state = str(row["state"] or "")
        result = _status_result(state, done={"succeeded"}, waiting={"blocked"}, running={"running"})
        action = str(row["action_code"] or "").replace("_", " ").lower()
        title = str(row["title"] or "")
        events.append(_event(
            key=f"execution-{row['id']}", kind="automatic_execution", what="automation", who="automatic",
            result=result, timestamp=str(row["completed_at"] or row["updated_at"] or row["created_at"] or ""),
            sentence=f"Automatic step “{action}” for {_book(title)}: {RESULTS[result].lower()}",
            title=title, author=str(row["author"] or ""), detail=str(row["error"] or ""),
            detail_href=f"/activity/recovery-plan/{row['plan_id']}",
        ))
    return events


_IMPORT_OUTCOMES = {
    "VERIFIED_CORRECT": ("done", "BookGuard verified it is the right book"),
    "WRONG_CONTENT": ("waiting", "it contains the wrong file"),
    "WRONG_MEDIA_TYPE": ("waiting", "it is the wrong kind of media"),
    "UNSAFE_FILE": ("waiting", "it failed the safety checks"),
    "METADATA_ERROR": ("waiting", "it is the right book with the wrong details"),
    "INSUFFICIENT_EVIDENCE": ("waiting", "BookGuard could not prove what it is"),
}


def _import_events(conn, limit: int) -> list[dict[str, Any]]:
    events = []
    for row in _rows(conn, "import_checks", "SELECT * FROM import_checks ORDER BY id DESC LIMIT ?", limit):
        title = str(row["title"] or "")
        lead = f"Bindery imported {_book(title)}"
        if row["error"]:
            result, outcome = "failed", "checking it failed"
        elif row["verdict"]:
            result, outcome = _IMPORT_OUTCOMES.get(str(row["verdict"]), ("waiting", "it needs a look"))
        elif row["classification"] == "PASS":
            result, outcome = "done", "it passed the check"
        else:
            result, outcome = "waiting", "the scan flagged it for review"
        events.append(_event(
            key=f"import-{row['id']}", kind="import_check", what="checks", who="bookguard", result=result,
            timestamp=str(row["checked_at"]), sentence=f"{lead}; {outcome}",
            title=title, author=str(row["author"] or ""), path=str(row["stored_path"] or ""),
            detail=str(row["error"] or ""),
            detail_href=f"/review#book-{row['result_id']}" if row["result_id"] and result == "waiting" else "",
        ))
    return events


def _describe_change(change: dict[str, Any]) -> str:
    label = change["label"]
    if "added" in change:
        parts = [f"added {', '.join(map(str, change['added']))}" if change["added"] else "",
                 f"removed {', '.join(map(str, change['removed']))}" if change["removed"] else ""]
        return f"{label}: " + "; ".join(p for p in parts if p)
    if change["key"] == "bindery_api_key_set":
        return f"{label} changed"
    fmt = lambda v: "on" if v is True else "off" if v is False else ("empty" if v in (None, "") else str(v))  # noqa: E731
    return f"{label}: {fmt(change.get('before'))} → {fmt(change.get('after'))}"


def _settings_events(conn, limit: int) -> list[dict[str, Any]]:
    events = []
    sql = "SELECT * FROM settings_changes ORDER BY id DESC LIMIT ?"
    for row in _rows(conn, "settings_changes", sql, limit):
        try:
            changes = json.loads(row["changes_json"] or "[]")
        except ValueError:
            changes = []
        described = [_describe_change(change) for change in changes]
        if row["action"] == "reset":
            sentence = f"You reset settings to their defaults ({len(changes)} changed)"
        elif len(described) == 1:
            sentence = f"You changed a setting: {described[0]}"
        else:
            sentence = f"You changed {len(described)} settings"
        events.append(_event(
            key=f"settings-{row['id']}", kind="settings", what="system", who="you", result="done",
            timestamp=str(row["changed_at"]), sentence=sentence,
            items=[{"text": text, "href": ""} for text in described] if len(described) > 1 or row["action"] == "reset" else [],
        ))
    return events


def _start_events(conn, limit: int) -> list[dict[str, Any]]:
    events = []
    for row in _rows(conn, "app_starts", "SELECT * FROM app_starts ORDER BY id DESC LIMIT ?", limit):
        revision, previous = str(row["revision"] or ""), str(row["previous_revision"] or "")
        version = f"v{row['version']}" + (f" ({revision[:7]})" if revision and revision != "unknown" else "")
        if not previous and not row["previous_version"]:
            sentence = f"BookGuard {version} started for the first time"
        elif revision != previous or row["version"] != row["previous_version"]:
            sentence = f"BookGuard was updated to {version}"
        else:
            sentence = f"BookGuard {version} restarted"
        events.append(_event(
            key=f"start-{row['id']}", kind="app_start", what="system", who="system", result="done",
            timestamp=str(row["started_at"]), sentence=sentence,
            detail=(f"Previously v{row['previous_version']} ({previous[:7]})" if previous and revision != previous else ""),
        ))
    return events


# ---- the timeline -----------------------------------------------------------

def activity_events(limit: int = 1000) -> list[dict[str, Any]]:
    history = operation_history(limit)["items"]
    verifications = [item for item in history if item["kind"] == "verification"]
    observations = [item for item in history if item["kind"] == "observe"]
    events = [event for item in history if item["kind"] not in {"verification", "observe"}
              if (event := _from_history(item))]
    events += _verification_days(verifications) + _observe_days(observations)
    with local_conn() as conn:
        for source in (_scan_events, _move_events, _execution_events, _settings_events, _start_events,
                       _import_events):
            events += source(conn, limit)
    events.sort(key=lambda event: (event["timestamp"], event["key"]), reverse=True)
    return events


def filter_events(
    events: list[dict[str, Any]],
    *,
    what: str = "",
    who: str = "",
    result: str = "",
    period: str = "",
    query: str = "",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter by kind of activity, actor, outcome ("problems"), period in days and text."""
    out = events
    if what in WHAT:
        out = [e for e in out if e["what"] == what]
    if who in WHO:
        out = [e for e in out if e["who"] == who]
    if result == "problems":
        out = [e for e in out if e["result"] in PROBLEM_RESULTS]
    elif result in RESULTS:
        out = [e for e in out if e["result"] == result]
    if period in PERIODS:
        since = ((now or datetime.now(timezone.utc)) - timedelta(days=PERIODS[period])).isoformat()
        out = [e for e in out if e["timestamp"] >= since]
    needle = query.strip().casefold()
    if needle:
        out = [e for e in out if needle in " ".join(
            [e["sentence"], e["title"], e["author"], e["path"], e["detail"], *(i["text"] for i in e["items"])]
        ).casefold()]
    return out


def activity_totals(events: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(event["result"] for event in events)
    return {
        "events": len(events),
        "problems": sum(counts[result] for result in PROBLEM_RESULTS),
        "waiting": counts["waiting"],
        "failed": counts["failed"],
    }
