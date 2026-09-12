# BookGuard

**BookGuard** is a conservative validation, quarantine, and metadata-repair companion for [Bindery](https://github.com/vavallee/bindery).

It answers two related questions:

> Does the file Bindery imported actually look like the book Bindery thinks it is?

> If the file is the correct book but its metadata is broken, can that metadata be repaired safely?

BookGuard is designed around **audit first, repair second, destructive actions last**.

## What v0.4 does

- Reads Bindery's `book_files`, books, and authors from the Bindery SQLite database in **read-only mode**.
- Audits audiobook associations with `ffprobe` and representative sampling across audiobook folders.
- Audits EPUB, PDF, MOBI, AZW/AZW3, CBZ ComicInfo.xml, RTF metadata, and explicit Title:/Author: headers in TXT files.
- Detects music/soundtrack imports, strong spoken-word semantic mismatches, missing tracked files, partial matches, and unsupported metadata formats.
- Supports configurable author aliases/pen names.
- Detects mechanically swapped EPUB title/author fields without loosening general matching rules.
- Shows expected and detected title/author side by side.
- Provides metadata repair modes: `off`, `preview`, and `safe`.
- Repairs confirmed EPUB title/creator metadata.
- Repairs common audiobook book-level tags with Mutagen for MP3, FLAC, M4A/M4B/MP4, OGG, and Opus while preserving track titles and narrator/Artist tags.
- Can set audiobook Album, Album Artist, and optionally Genre from the confirmed Bindery assignment.
- Re-reads repaired metadata and verifies the write before reporting success.
- Stores before/after metadata in `/config/bookguard.db` and provides a Repairs page with guarded Undo.
- Refuses to metadata-repair `REJECT` or `MISSING` results.
- Keeps PDF, MOBI/AZW/AZW3, and other ebook metadata writing disabled for now; those formats remain inspection-only until their write paths are proven safe.
- Can optionally detach a tracked path using Bindery's API.
- Can optionally detach and move a single, non-shared tracked path into quarantine.

BookGuard **never automatically deletes media**.

## What v0.5 is adding

The `v0.5.0-automatic-maintenance` branch builds automatic maintenance as a sequence of independently guarded slices:

- Read-only Bindery status, replacement search, and candidate evaluation.
- Immediate re-verification before a WRONG_CONTENT mutation.
- Quarantine-first removal, exact native deregistration, rollback attempts, provenance-aware blocklisting, and replacement search.
- A fail-closed external-import readiness gate with a dedicated staging mount outside the library and quarantine roots.
- Read-only staged ebook inventory and staged-byte verification against one explicit Bindery book.
- SHA-256 and file-stat stability checks across verification.
- A staged file is marked `safeToAdmit` only for a stable `VERIFIED_CORRECT` result at 99% confidence.

Automatic queue grabbing and library admission remain disabled. A read-only `safeToAdmit` result is not a durable authorization token; the future admission operation must repeat verification at its own mutation boundary.

Read-only staging endpoints:

```text
GET  /api/automatic/staging/files
POST /api/automatic/books/{book_id}/staged-verification
```

The verification request uses a path relative to `/staging`:

```json
{"relativePath":"Bel Canto - Ann Patchett.epub"}
```

Absolute paths, traversal outside the staging root, symlinked files, empty files, unsupported formats, and files over the configured size limit are rejected.

## Safety model

Three independent safety systems exist.

### Bindery actions

```env
BOOKGUARD_ALLOW_ACTIONS=false
```

This controls Detach and Quarantine only. `PASS` results remain protected from those actions.

### Metadata repair

```env
BOOKGUARD_METADATA_REPAIR_MODE=preview
```

The default `preview` mode builds repair proposals but never writes media. `safe` mode permits only repairs that pass BookGuard's conservative identity rules, and only if the relevant media mount is writable.

The supplied Compose file deliberately mounts ebooks and audiobooks read-only:

```yaml
- /mnt/user/data/media/audiobooks:/audiobooks:ro
- /mnt/user/data/media/books:/books:ro
```

So even if Safe mode is selected accidentally, Docker still blocks writes. Remove `:ro` only when you deliberately want metadata repair to modify that library.

### Automatic reacquisition

```env
BOOKGUARD_STAGING_ROOT=/staging
BOOKGUARD_BINDERY_DROP_FOLDER=/data/bookguard-staging
BOOKGUARD_AUTOMATIC_REACQUISITION=false
BOOKGUARD_MAX_STAGED_EBOOK_BYTES=536870912
```

Automatic reacquisition fails closed unless every readiness check passes. Keep it disabled until Bindery is deliberately configured for external import and BookGuard's controlled-admission slice is complete.

## Metadata repair philosophy

BookGuard distinguishes **bad metadata** from **wrong content**.

A safe repair candidate might look like:

```text
Bindery:  Bel Canto — Ann Patchett
Embedded title:  Patchett, Ann
Embedded author: Bel Canto
```

BookGuard can recognize the swapped fields, preview the correction, write the canonical title/author, verify the result, record the before/after values, and allow Undo.

A wrong-content import such as:

```text
Bindery: Cat of Death! — Aaron Blabey
Embedded: Death of a Texan — Cat Hickey
```

is **not** repaired. BookGuard will not relabel unrelated content to make it appear correct.

For audiobooks, BookGuard currently changes only book-level fields selected in Settings. Track/chapter Title and narrator/Artist are preserved.

## Unraid quick start

```bash
cd /mnt/cache/appdata
git clone git@github.com:foobarbigtime/bookguard.git
cd bookguard
cp .env.example .env
docker compose up -d --build
```

Open:

```text
http://<unraid-ip>:8788
```

The supplied Compose file uses:

```text
/mnt/cache/appdata/bindery
/mnt/user/data/media/audiobooks
/mnt/user/data/media/books
/mnt/cache/appdata/bookguard
/mnt/user/data/bookguard-quarantine
/mnt/user/data/bookguard-staging
```

For the proven shared staging topology:

```text
Host:       /mnt/user/data/bookguard-staging
BookGuard:  /staging
Bindery:    /data/bookguard-staging
```

## Settings

Most behavior can be changed at `/settings` and is persisted in `/config/bookguard.db`.

Metadata repair settings include:

- repair mode: Off / Preview only / Safe repairs enabled
- audiobook and ebook repair enable/disable
- canonical normalization for confirmed PASS items
- set audiobook Album from Bindery title
- set audiobook Album Artist from Bindery author
- optional audiobook Genre and configurable genre value

Repair history and Undo are available at `/repairs`.

## Updating

```bash
cd /mnt/cache/appdata/bookguard
git pull
docker compose up -d --build
```

After scanner/matcher upgrades, run a **new scan**. Historical scan rows are kept and are not silently reclassified.

## Matching philosophy

BookGuard favors `REVIEW` when evidence is weak. Automatic `REJECT` remains deliberately narrow:

- **MUSIC_MISMATCH** — audio is explicitly music-like and does not support the expected author.
- **STRONG_MISMATCH** — stable embedded title + author metadata identifies a different spoken-word work.

Generic labels such as `Chapter 01` and `Track 01` are never enough for strong semantic rejection.

Metadata repair is narrower still: only already-confirmed identities or explicitly recognized mechanical metadata faults are eligible for Safe repair.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
PYTHONPATH=. pytest -q
uvicorn app.main:app --reload --port 8788
```

The test suite includes end-to-end synthetic EPUB tests covering preview, safe repair, verification, repair-history persistence, Undo, and staged-byte safety gates.

### Project structure

BookGuard has one application entry point and separates HTTP transport from
domain behavior:

```text
app/main.py                 Application composition and startup
app/routes/                 Page and API routers grouped by feature
app/services/dashboard.py  Dashboard view-model assembly
app/scanner.py              Library scan orchestration
app/verifier.py             Verification jobs, persistence, and repairs
app/verification_engine.py  Pure ebook identity extraction/classification
app/staging.py              Read-only staged-byte verification
app/automatic.py            Guarded automatic-maintenance workflow
app/bindery_client.py       Bindery API and API-key discovery
app/file_safety.py          Shared filesystem hashing/safety helpers
```

Versioned entry-point and verifier wrappers are intentionally avoided. New
behavior should be added to the relevant router or service and covered by a
focused test instead of layering another `main_v*` or `verifier_v*` module.
