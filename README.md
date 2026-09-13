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
- Opt-in direct ebook admission to the exact former Bindery path, followed by Bindery library reconciliation.
- Snapshot verification on the destination filesystem, atomic no-overwrite publication, and durable recovery state.
- One-at-a-time, explicitly selected Bindery acquisition with durable queue and staged-verification state.

Unattended scheduling remains disabled. Controlled acquisition and direct admission are separate opt-ins with separate confirmations. Admission repeats verification on a private copied snapshot at its own mutation boundary; a prior `safeToAdmit` response is never treated as authorization.

Read-only staging endpoints:

```text
GET  /api/automatic/staging/files
POST /api/automatic/books/{book_id}/staged-verification
GET  /api/automatic/admission-readiness
GET  /api/automatic/admissions
POST /api/automatic/results/{result_id}/admit-staged-ebook
POST /api/automatic/admissions/{admission_id}/reconcile
GET  /api/automatic/acquisition-readiness
GET  /api/automatic/acquisitions
POST /api/automatic/results/{result_id}/acquisitions
POST /api/automatic/acquisitions/{acquisition_id}/reconcile
POST /api/automatic/acquisitions/{acquisition_id}/admit
POST /api/automatic/acquisitions/{acquisition_id}/finalize
```

The verification request uses a path relative to `/staging`:

```json
{"relativePath":"Bel Canto - Ann Patchett.epub"}
```

Absolute paths, traversal outside the staging root, symlinked files, empty files, unsupported formats, and files over the configured size limit are rejected.

Acquisition finalization is a separate, explicitly confirmed operation. It is
available only after the linked admission is registered at the exact Bindery
path and the staged SHA-256 still matches. It removes the terminal Bindery
queue record with download-client and downloaded-data deletion disabled, then
removes only the verified staging copy. Library bytes and durable audit records
are retained.

## Safety model

BookGuard uses layered access, repair, action, and reacquisition safeguards.

### Application access

BookGuard requires HTTP Basic authentication for every page and API route
except `/health`:

```env
BOOKGUARD_AUTH_USERNAME=bookguard
BOOKGUARD_AUTH_PASSWORD=replace-with-a-long-random-password
```

The container refuses to start without a password. The supplied Compose file
also binds to localhost by default:

```env
BOOKGUARD_BIND_ADDRESS=127.0.0.1
```

For direct LAN access, set that value to the Unraid server's LAN address. Basic
authentication controls access but does not encrypt traffic. Keep plain HTTP on
a trusted LAN; use an HTTPS reverse proxy or VPN for remote access, and do not
publish BookGuard directly to the internet.

State-changing repair, undo, scan, reset, detach, quarantine, remediation,
admission, and reconciliation requests also require an explicit
operation-specific confirmation in the JSON body. Authentication and
confirmation serve different purposes and both are enforced by the server.

### Bindery actions

```env
BOOKGUARD_ALLOW_ACTIONS=false
```

This controls every media-changing operation, including Detach, Quarantine,
direct admission, and admission reconciliation. `PASS` results remain protected
from destructive actions.

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

Automatic reacquisition fails closed unless every readiness check passes. The
implemented workflow is deliberately operator-driven: it can start one freshly
revalidated release, observe its Bindery queue record, and verify exactly one
ebook arriving in an initially empty staging folder. There is no scheduler,
automatic candidate choice, automatic retry, unattended staged-file deletion,
or automatic admission.

Starting an acquisition additionally requires Bindery auto-grab to be disabled,
a complete and idle Bindery queue, an empty and completely inventoried staging
folder, no other unfinished acquisition, and `BOOKGUARD_ALLOW_ACTIONS=true`. The
selected release must still appear exactly once in a fresh search, explicitly be
an ebook, pass BookGuard's independent title/author gate, and not have an exact
grabbed-and-imported match in Bindery history. BookGuard records the session
before asking Bindery to grab it.

Reconciliation never moves or deletes staged files. It accepts a staged result
only after Bindery reaches a completed external-handoff state, the folder
contains exactly one supported ebook, and its bytes verify at the admission
threshold. Any ambiguity becomes durable `review_required` state. A third,
separately confirmed request hands that verified session to the existing
admission transaction, which checks the hash and repeats full verification
before publication.

### Controlled ebook admission

Direct admission uses a separate writable alias while the ordinary `/books`
mount remains read-only:

```env
BOOKGUARD_ADMISSION_ENABLED=false
BOOKGUARD_ADMISSION_ROOT=/admission-books
BOOKGUARD_ADMISSION_BINDERY_ROOT=/data/media/books
```

Start the opt-in topology only when deliberately testing admission:

```bash
docker compose -f compose.yaml -f compose.admission.yaml up -d --build
```

An admission is tied to a prior scan result so BookGuard can reuse the exact
former library path instead of reproducing Bindery's configurable naming
engine. BookGuard refuses missing/symlinked destination directories, format
changes, existing destinations, inconsistent root mappings, changed Bindery
book identity, or an already-registered ebook.

At the mutation boundary BookGuard opens the staged file without following
symlinks, copies it to a private file on the destination filesystem, verifies
the copied bytes again at 99% confidence, flushes them, and atomically publishes
without overwrite. BookGuard prefers Linux `renameat2(RENAME_NOREPLACE)` and,
on filesystems such as Unraid `shfs`, falls back to atomically linking the private
verified snapshot into place before immediately removing its private name. The
library file is never hardlinked to staging and remains independent from it.
Admission state is recorded in `/config/bookguard.db` before copying, including
the successful publication method. If publication succeeds but a later
durability step reports an error, the record remains recoverable rather than
being mislabeled as an ordinary failed copy. After publication, BookGuard asks
Bindery to scan its library and retains the staged source even after registration
is confirmed. A separate `FINALIZE_EBOOK_ACQUISITION` request can then verify the
registered library copy and staged hash, remove only the terminal Bindery queue
record with download-client and file deletion explicitly disabled, and remove
only that acquisition's verified staging copy. The library file and durable
admission/acquisition audit records are retained. Interrupted finalization stays
blocked as `cleanup_required` unless the exact safe cleanup state can be proven.
An explicitly finalized historical record may also adopt cleanup that an
operator already completed, but only after proving the registered library hash,
complete queue response, absent exact queue record, and safely absent staged
path; that recovery changes the audit status only.

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
# Edit .env and set BOOKGUARD_AUTH_PASSWORD before starting.
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

Before deploying this security update, add the required access credentials to
the existing `.env`. To retain direct LAN access, also set the bind address to
the Unraid server's LAN IP:

```env
BOOKGUARD_AUTH_USERNAME=bookguard
BOOKGUARD_AUTH_PASSWORD=replace-with-a-long-random-password
BOOKGUARD_BIND_ADDRESS=192.168.1.10
```

```bash
cd /mnt/cache/appdata/bookguard
git pull
docker compose up -d --build
```

## Isolated smoke test

Before a release or live acceptance test, run:

```bash
./scripts/smoke-test.sh
```

The command builds the current source and validates both the normal Compose
topology and the opt-in admission overlay. It then exercises the complete ebook
workflow—acquisition, external-handoff enforcement, staged-byte verification,
atomic admission, registration, and finalization—against a fake Bindery client
and temporary files.

The workflow container uses `--network none`, a read-only root filesystem, and
an isolated temporary filesystem. It does not mount `/config`, `/books`,
`/staging`, the Bindery database, the download client, or any other live host
path. The temporary library and audit database are removed when the command
finishes. A successful run therefore replaces most of the long manual CLI
checks; a live test is still appropriate once per release milestone.

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

The test suite includes end-to-end synthetic EPUB tests covering preview, safe
repair, verification, repair-history persistence, Undo, staged-byte safety
gates, and the isolated acquisition-to-finalization lifecycle.

### Project structure

BookGuard has one application entry point and separates HTTP transport from
domain behavior:

```text
app/main.py                 Application composition and startup
app/routes/                 Page and API routers grouped by feature
app/services/dashboard.py  Dashboard view-model assembly
app/scanner.py              Library scan orchestration
app/verifier.py             Verification jobs, persistence, and repairs
app/ebook_extraction.py     Ebook metadata and text extraction
app/verification_engine.py  Ebook identity classification and safety rules
app/verification_constants.py Shared verification limits
app/staging.py              Read-only staged-byte verification
app/admission.py            Atomic direct-admission transaction and recovery
app/acquisition.py          One-at-a-time Bindery queue-to-staging workflow
app/automatic.py            Guarded automatic-maintenance workflow
app/bindery_client.py       Bindery API and API-key discovery
app/file_safety.py          Shared filesystem hashing/safety helpers
tools/smoke_test.py         Isolated workflow and Compose safety harness
scripts/smoke-test.sh       One-command containerized smoke-test runner
```

Versioned entry-point and verifier wrappers are intentionally avoided. New
behavior should be added to the relevant router or service and covered by a
focused test instead of layering another `main_v*` or `verifier_v*` module.
