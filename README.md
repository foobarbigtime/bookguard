# BookGuard

**BookGuard** is a conservative validation, quarantine, and metadata-repair companion for [Bindery](https://github.com/vavallee/bindery).

### Shared-file registration conflicts

Triage provides **Check shared files** to detect different Bindery books tracking
hard links to the same ebook. Discovery and **Verify correction preview** are
read-only and remain available with Bindery actions disabled.

The first guarded correction supports a Bindery `.bindery-stage-` EPUB alias
incorrectly owned by a different book while a final EPUB in the same folder is
already correctly registered. It requires exactly two tracked paths and two
physical hard links, all core deterministic checks enabled and passing, native
title-page evidence verifying the retained book at 99% confidence, agreement
between Bindery's API and database, disabled Bindery auto-grab, external import
mode, and no active queue records. Equivalent catalog titles, competing primary
title evidence, symlinks, extra hard links, and changed previews are blocked.

**Remove wrong ebook association** requires enabled Bindery actions and explicit
per-item confirmation. BookGuard uses Bindery's exact-path deregistration API;
it does not reassign, delete, move, or rewrite media, request a scan, or start a
download. Both filenames remain in place. A full proof is persisted before the
request, then BookGuard verifies the retained ebook, both books' remaining file
registrations, audiobook paths, hard-link identity, and SHA-256 after the request.
No writable library alias, admission gate, or automatic-reacquisition gate is
required for this database-only operation; the existing Bindery action gate
still applies and stays off by default.

Interrupted or uncertain operations remain visible in the panel's history and
block further corrections. **Recheck interrupted correction** only observes
current state: it records an already completed operation as applied when all
postconditions pass, or cancels an unchanged operation when the original proof
still matches. It never repeats the Bindery mutation. Changed or unprovable state
continues to require review.

After an applied correction, **Verify staging-link cleanup** checks that the
staging name is unregistered everywhere and still shares the exact recorded
inode and SHA-256 with the protected final EPUB. **Remove unregistered staging
link** is a separate explicit action. It requires the existing Bindery action
gate, `BOOKGUARD_EBOOK_ACTIONS_ENABLED=true`, and the opt-in
`compose.actions.yaml` writable alias; `/books` remains read-only. No admission
or automatic-reacquisition gate is needed.

Cleanup unlinks only the exact proven staging name through the writable alias,
using directory handles with symlink following disabled. It does not change
Bindery registrations, move files, remove directories, or sweep sibling names.
BookGuard verifies the alias is absent, the final EPUB retains the same inode,
size, mtime, and SHA-256 with one remaining link, and both books' ebook/audiobook
registrations are unchanged. A cleanup journal is persisted before unlink.
**Recheck interrupted staging cleanup** observes the recorded state without
repeating unlink or recreating a link; unchanged work is cancelled and a proven
completed unlink is adopted. Uncertain or changed state remains blocked for
review. Remove the unused staging name before initiating another library scan
so it cannot be imported again against a wrong identity.

It answers two related questions:

> Does the file Bindery imported actually look like the book Bindery thinks it is?

> If the file is the correct book but its metadata is broken, can that metadata be repaired safely?

BookGuard is designed around **audit first, repair second, destructive actions last**.

## What v0.6 adds

v0.6 adds staged, fail-closed automation on top of the v0.5 workflows. The
default remains `BOOKGUARD_AUTOMATION_MODE=manual` with an empty
`BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST`, so upgrading changes no behavior until
an operator opts in.

- **Observe Mode** records durable decisions about recovery work without any
  Bindery, queue, staging, quarantine, metadata, or library mutation.
- **Unified media evidence** extends content verification to audiobooks and
  detects ebook/audio cross-assignment from the actual bytes and containers.
- **Recovery classification and durable recovery plans** describe each
  proposed step, including which steps would perform an external mutation.
- **Supervised Automatic Mode (E4)** advances one work item per explicitly
  confirmed cycle and attempts at most one external mutation per cycle. An
  action runs only when it is allowlisted, has a registered executor, and passes
  fresh identity, byte, destination, and operation-specific checks. Cycles are
  serialized, and an interrupted external effect is held for read-only
  reconciliation rather than replayed.
- **E4 executors** cover bounded transient grab retry, known-queue staging
  progression, verified-acquisition admission, guarded publication recovery
  and scan requests, retirement of proven pre-publication failures,
  registration-conflict correction, and guarded finalization. Alternate and
  post-quarantine replacement grabs require an explicit operator choice of
  release; E4 never selects a candidate on its own.
- **Unsafe-media quarantine** moves proven unsafe ebooks into quarantine,
  verifies cross-mount copies before removing a source, proves custody before
  any replacement grab, and proves the replacement's final state before
  closing the plan.
- **Admission publishes only proven bytes.** Live authorization checks run
  before the private snapshot is sealed; the sealed snapshot is re-verified
  through a no-follow descriptor immediately before publication; and the
  published library file is re-hashed before success is recorded. A mismatch
  blocks the plan for operator review and leaves the file in place.
- **Attention** collects items that need an operator, including E4 execution
  receipts left running after an interruption, with read-only guidance.

See [Observe Mode](#observe-mode-v06-foundation) and
[Supervised Automatic Mode (E4)](#supervised-automatic-mode-e4) for
configuration and endpoints.

## Core capabilities (since v0.4)

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

## What v0.5 added

v0.5 built automatic maintenance as a sequence of independently guarded slices:

- Read-only Bindery status, replacement search, and candidate evaluation.
- Immediate re-verification before a WRONG_CONTENT mutation.
- Quarantine-first removal, exact native deregistration, rollback attempts, provenance-aware blocklisting, and replacement search.
- A fail-closed external-import readiness gate with a dedicated staging mount outside the library and quarantine roots.
- Read-only staged ebook inventory and staged-byte verification against one explicit Bindery book.
- SHA-256 and file-stat stability checks across verification.
- Default-on archive safety checks before EPUB/CBZ extraction.
- Ebook verification opens sources with no-follow semantics, snapshots stable bytes into private /config storage, runs every parser and ClamAV against that same snapshot, then re-checks the original path before accepting the verdict.
- The normal BookGuard container uses a read-only root filesystem; only explicit data mounts and a bounded no-exec /tmp tmpfs are writable.
- Optional fail-closed ClamAV malware scanning through a deployment-only clamd endpoint.
- A staged file is marked `safeToAdmit` only for a stable `VERIFIED_CORRECT` result at 99% confidence.
- Opt-in direct ebook admission to the exact former Bindery path, followed by Bindery library reconciliation.
- Snapshot verification on the destination filesystem, atomic no-overwrite publication, and durable recovery state.
- One-at-a-time, explicitly selected Bindery acquisition with durable queue and staged-verification state.
- An opt-in supervised coordinator that resumes operator-started work after restart and pauses for explicit admission.
- A Triage-page replacement workflow for guarded preparation, candidate choice, progress, explicit admission, and finalization.
- Exact-path registration-conflict detection that stops scan loops when Bindery assigns an admitted ebook to the wrong book.
- Explicit, guarded exact-path Bindery association correction with durable interrupted-operation recovery.

Unattended candidate selection remains disabled. Controlled acquisition and direct admission are separate opt-ins with separate confirmations. Admission repeats verification on a private copied snapshot at its own mutation boundary; a prior `safeToAdmit` response is never treated as authorization.

Automatic-maintenance endpoints (status and inventory `GET` requests are read-only):

```text
GET  /api/automatic/staging/files
POST /api/automatic/books/{book_id}/staged-verification
GET  /api/automatic/admission-readiness
GET  /api/automatic/admissions
POST /api/automatic/results/{result_id}/admit-staged-ebook
POST /api/automatic/admissions/{admission_id}/reconcile
POST /api/automatic/admissions/{admission_id}/correct-registration
GET  /api/automatic/acquisition-readiness
GET  /api/automatic/acquisitions
GET  /api/automatic/acquisition-coordinator
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

Because browsers resend Basic credentials automatically, every state-changing
request (anything other than `GET`/`HEAD`/`OPTIONS`) is also rejected with
`403` when the browser marks it cross-site, when its `Origin` does not match the
request host, or when it has a body that is not `application/json`. Scripts that
call the API with `curl -H 'Content-Type: application/json'` are unaffected. If
BookGuard sits behind a reverse proxy that rewrites the `Host` header, list the
public origin(s) the browser uses:

```env
BOOKGUARD_TRUSTED_ORIGINS=https://bookguard.example.com
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

Guarded ebook quarantine and wrong-content remediation do not require making
`/books` writable. They use a separate, independently gated alias:

```env
BOOKGUARD_EBOOK_ACTIONS_ENABLED=false
BOOKGUARD_EBOOK_ACTION_ROOT=/action-books
```

Include `compose.actions.yaml` only for a deliberate ebook action session. The
read-only source path and writable alias must have identical library-relative
paths and must resolve to the same device and inode before BookGuard will move
anything. A failed move occurs before Bindery is changed; a later detach failure
causes BookGuard to restore the file through the writable alias.

```bash
bash scripts/build-with-provenance.sh
docker compose -f compose.yaml -f compose.actions.yaml up -d --no-build
```

### Moving a misfiled ebook to the right book

Sometimes verification shows that an ebook filed under one book is really
another catalogued book, and that book has no ebook of its own. For example, a
file imported for a truncated *Kill* entry is really *Kill Alex Cross*. The fix
is not a download: Bindery can move the file to the book it really is.

In Review, and on the Triage page, a `WRONG_CONTENT` verification for such a file gains a
**Move to correct book** button. It opens a read-only preview showing the book
the file is filed under, the book it really belongs to, and the current and new
paths. If anything makes the move unsafe, the preview lists the reasons instead
of offering the move.

The preview, and the book's panel in Review, also show the language the file
declares about itself (from its EPUB or PDF metadata, recorded by the library
scan). A file in another language can be exactly the book it says it is, such
as a Swedish edition catalogued as its own Bindery book, so language never
blocks the move; the preview warns instead, because moving keeps the
translation. Review's **Not in English** filter lists every open book whose
file declares a language other than English.

The move requires `BOOKGUARD_ALLOW_ACTIONS=true` and explicit
`MOVE_TO_CORRECT_BOOK` confirmation. At the moment of acting BookGuard verifies
the file again (including the malware scan) and requires that:

- the file is still proven to be the other book's missing ebook;
- Bindery's import mode is normal, not `external` (which skips file moves);
- the right book still has no ebook;
- Bindery tracks the file under exactly the book it was filed as;
- Bindery's own preview reports a move to a path inside the ebook library.

BookGuard records the plan, asks Bindery once to reassign the exact path, and
then confirms that the right book tracks the new path, the old book no longer
tracks the old one, and the file at the new path has the verified SHA-256.
Bindery performs the move; BookGuard never writes to the library itself and
does not need the writable action alias. The book the file was filed under is
left without an ebook; the Replacement workflow can fetch one for it.

Bindery moves files in the background. If it has not finished within two
minutes the move stays pending, and **Check again** in the Triage page's
**Moves to the right book** history re-checks it read-only, without asking
Bindery again. The same check is available from
`POST /api/catalogue-moves/{id}/reconcile`, and the history from
`GET /api/catalogue-moves`.

### Checking new imports

BookGuard checks each file Bindery imports soon after it appears, instead of
waiting for the next full library scan. Every few minutes (5 by default,
**Settings → Scanning**) it looks in Bindery's database, read-only as the scan
does, for files added since it last looked. Each new file is scanned exactly as
a full scan would scan it and added to the latest scan, so it appears in
Review and on Home; an ebook the scan flags is also verified. Activity records
the outcome, for example "Bindery imported “The Client”; it contains the wrong
file". Nothing in Bindery or the library is changed.

The first check starts from that moment (the full scan covers what is already
there), checks wait while a full scan runs, and a file is checked once. Turn
it off with `BOOKGUARD_WATCH_IMPORTS=false` or in Settings.

**Optional: instant checks with Bindery's webhook.** Bindery can tell BookGuard
the moment it imports something. In Bindery, add a notification under
**Settings → Notifications** with:

- URL `http://<bookguard-host>:8788/api/bindery/webhook`
- **On import** and **On upgrade** enabled
- a custom header `Authorization` with the value `Basic ` followed by the
  base64 of `bookguard:<your BookGuard password>`
  (`echo -n 'bookguard:password' | base64`)

Bindery refuses webhooks to private (LAN) addresses unless its container has
`BINDERY_NOTIFICATIONS_ALLOW_PRIVATE=true`. The webhook only wakes the check
early: BookGuard still reads Bindery's database to decide which files are new,
so without it the timer does the same work a few minutes later.

### Scheduled scan

A full library scan can run on a schedule: **Settings → Scanning →
Scheduled library scan** (off, daily or weekly, at a set time in the
container's local time; set `TZ` on the container to use yours), or
`BOOKGUARD_SCAN_SCHEDULE`, `BOOKGUARD_SCAN_SCHEDULE_TIME` and
`BOOKGUARD_SCAN_SCHEDULE_DAY` (0 = Monday … 6 = Sunday). It is off by default.
**System** lists every scheduled task with its last and next run and a
**Run now** button, and its Health section explains any problem and how to fix
it.

### Observe Mode (v0.6 foundation)

Observe Mode is the first stage of BookGuard's safe automation work. It is
deployment-only and fail-closed:

```env
BOOKGUARD_AUTOMATION_MODE=manual
```

Supported values are `manual`, `observe`, and `automatic`; any other value is a
configuration error. `manual` preserves the explicitly confirmed workflow.
`observe` records durable decisions about recovery work using existing
BookGuard database state, but does not invoke Bindery mutation, queue mutation,
staging cleanup, quarantine, metadata writes, or library publication.

While `observe` is active, the supervised acquisition coordinator is disabled
even if its older mutation gates are enabled. Legacy mutation endpoints under
`/api/automatic` return a conflict instead of running; the explicit observe
cycle only writes BookGuard's decision journal. Read-only inspection and
verification endpoints remain available.

Observe decisions are idempotently journaled in `automation_observations`,
appear in unified History, and the latest decision for a subject appears in
Attention when the decision is `attention`. Missing durable ebook verification
is recorded as `would_verify_result` rather than Attention because verification
is a read-only prerequisite that a future Automatic Mode can perform safely.
Observe Mode itself still does not open the library file. A later safe/terminal
state records a resolving `no_action` decision so stale Observe Mode attention
does not remain active.

```text
GET  /api/automatic/observe
POST /api/automatic/observe/run
```

The run endpoint requires the exact confirmation token
`RUN_OBSERVE_MODE`. Running it may write only BookGuard's own durable decision
journal; the source scan, verification, acquisition, admission, repair, cleanup,
Bindery, queue, staging, quarantine, and library state are not changed by the
Observe decision engine.

#### Unified media evidence (v0.6 E2)

The v0.6 evidence engine extends content verification to audiobooks and detects
media-kind mismatches from the actual bytes/containers rather than trusting the
Bindery format or filename. Common audiobook containers discovered by BookGuard
are probed with ffprobe, including MP3, M4A/M4B, FLAC, AAC, Ogg/Opus, WAV, MP4,
WMA, AIFF, APE, MKA, AC3, AMR, AU, CAF, AA, and AAX.

Audiobook evidence combines:

- deterministic container/codec readability and duration checks;
- track/file count, chapter count, sample-rate/channel/codec consistency;
- filename disc/track continuity checks;
- embedded title/album, author/artist/album-artist, narrator/performer,
  language, track, and disc tags;
- cross-file identity consensus;
- actual-media detection for ebook/audio cross-assignment;
- durable media-set fingerprints for idempotent verification.

The same `content_verifications` journal now accepts audiobook evidence.
A proven readable audiobook that matches the expected title/author becomes
`VERIFIED_CORRECT`; strong consistent different identity becomes
`WRONG_CONTENT`; unreadable/corrupt expected audio becomes `UNSAFE_FILE`;
and deterministic ebook/audio cross-assignment becomes `WRONG_MEDIA_TYPE`.
Weak or contradictory identity evidence remains `INSUFFICIENT_EVIDENCE`.

Observe Mode treats missing audiobook evidence as `would_verify_audiobook`
instead of Attention. Proven wrong content, wrong media kind, unsafe media, and
safe metadata repairs are also represented as proposed future automatic work,
while true insufficient evidence remains Attention. These E2 decisions still
perform no library or Bindery mutation.

### Automatic reacquisition

```env
BOOKGUARD_AUTOMATION_MODE=manual
BOOKGUARD_STAGING_ROOT=/staging
BOOKGUARD_BINDERY_DROP_FOLDER=/data/bookguard-staging
BOOKGUARD_AUTOMATIC_REACQUISITION=false
BOOKGUARD_ACQUISITION_COORDINATOR_ENABLED=false
BOOKGUARD_ACQUISITION_COORDINATOR_INTERVAL_SECONDS=10
BOOKGUARD_MAX_STAGED_EBOOK_BYTES=536870912
```

The manual acquisition workflow fails closed unless every readiness check
passes. It is operator-driven: it can start one freshly revalidated release,
observe its Bindery queue record, and verify exactly one ebook arriving in an
initially empty staging folder. Its coordinator does not choose candidates,
retry a failed grab, or admit a verified ebook. E4 has separately gated
recovery steps described below.

The optional supervised coordinator removes the need to press Reconcile
repeatedly. It discovers the single durable active acquisition after startup,
polls Bindery, observes stable staging bytes, and advances verification. It
stops at `verified` with `explicitAdmissionRequired`; it never calls the
admission operation. After an operator explicitly admits those verified bytes,
the coordinator can resume registration reconciliation and finalization. It
removes only the exact terminal Bindery queue record and verified staging copy,
with download-client and downloaded-data deletion disabled.

The coordinator starts only when its own opt-in, automatic reacquisition, and
guarded actions are enabled. Turning off either mutation gate makes it remain
blocked without changing durable acquisition state. Its read-only status is
available from `GET /api/automatic/acquisition-coordinator`. The polling
interval is clamped between 2 and 300 seconds.

The Triage page exposes this workflow without changing its safety policy. A
`WRONG_CONTENT` verification gains a Replacement workflow button. The browser
can run the read-only preflight, explicitly prepare the exact bad file, display
safe and rejected search candidates, start one selected release, and show the
durable acquisition state. It pauses for an explicit Admit verified ebook
decision. When the coordinator is disabled, equivalent manual progress,
registration, and finalization controls remain available. The UI never enables
environment or action gates on the operator's behalf. A prepared MISMATCH ebook
whose source is now absent retains a Resume replacement entry after refresh;
the backend still revalidates the missing path and Bindery association before
allowing a release to start.

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

An operator can explicitly retry staged verification from `review_required`
after verifier rules are updated. The coordinator does not retry that state on
its own, and the staged file remains untouched unless it later passes the same
admission threshold.

### Supervised Automatic Mode (E4)

`BOOKGUARD_AUTOMATION_MODE=automatic` enables the E4 recovery executor, which
advances one durable work item per confirmed `POST /api/automatic/run` request
(`{"confirm":"RUN_AUTOMATIC_CYCLE"}`). It attempts at most one external
mutation per cycle. The default mode is `manual`, and the default
`BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST` is empty. An action runs only when its
code is allowlisted, a live executor is registered, and its fresh identity,
byte, destination, and operation-specific gates pass. A supported action code
alone does not mean that a live executor exists. Inspect
`GET /api/automatic/execution-policy` for the mode, allowlist, and registered
executors; `GET /api/automatic/executions` shows the durable execution journal.

E4 can retry a bounded failed acquisition grab, advance a known Bindery queue
item to verified staging, and admit separately allowlisted verified bytes. It
also has guarded publication, scan, registration, and quarantine recovery steps.
Candidate selection requires an explicit operator choice; the executor does not
choose a new release on its own. Publication uses no-replace and exact-byte
checks, and uncertain interrupted external effects are held for reconciliation
instead of being blindly retried. Existing action gates, including
`BOOKGUARD_ALLOW_ACTIONS`, `BOOKGUARD_AUTOMATIC_REACQUISITION`, and
`BOOKGUARD_ADMISSION_ENABLED` where applicable, remain separate prerequisites.
Set the mode back to `manual` or clear the allowlist to stop new E4 executions;
the journal remains available for review.

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
bash scripts/build-with-provenance.sh
docker compose -f compose.yaml -f compose.admission.yaml up -d --no-build
```

For the complete supervised replacement workflow, include both narrow aliases;
`/books` remains read-only throughout:

```bash
bash scripts/build-with-provenance.sh
docker compose \
  -f compose.yaml \
  -f compose.actions.yaml \
  -f compose.admission.yaml \
  up -d --no-build
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

Before requesting any follow-up library scan, reconciliation checks Bindery's
read-only database for the exact admitted path. If a different book owns that
path, the admission enters durable `registration_conflict` state, the
coordinator stops polling it, and no additional scan is requested. Triage shows
the conflicting state and offers either a recheck after an external correction
or a separately confirmed guarded correction. The guarded correction repeats
the library and staging hashes, intended-book identity, exact wrong owner,
complete queue, disabled auto-grab, and Bindery no-move preview checks. It removes
only the linked queue record with client and file deletion disabled, temporarily
uses Bindery's manual reassignment operation, and restores external import mode.
Library and staged bytes are never moved or deleted. An interruption remains in
durable `registration_correcting` state; the coordinator pauses and only another
explicit operator request may resume it.

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
bash scripts/build-with-provenance.sh
docker compose up -d --no-build
```

Use `scripts/build-with-provenance.sh` for every BookGuard image rebuild. It
stamps the image with the application version, exact Git revision, and source
repository, and refuses to stamp tracked uncommitted changes. After that build
completes, deploy with `--no-build`. A direct `docker compose ... --build`
can create an image whose revision label is `unknown`, so it is intentionally
not the documented deployment path.

Open:

```text
http://<unraid-ip>:8788
```

The supplied Compose file keeps BookGuard's persistent database outside the
Git checkout. By default it uses:

```text
/mnt/cache/appdata/bookguard-config
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

Verification and security settings include three default-on, read-only checks:

- the file signature must match the ebook extension when the format has a fixed signature
- EPUB/CBZ archives reject traversal paths, symlinks, encryption, duplicate or case-colliding names, excessive member counts and sizes, and dangerous expansion ratios
- EPUB files must pass ZIP CRC, mimetype, container, package, manifest, and spine validation
- PDF files must have a valid header, cross-reference structure, page tree, and EOF marker
- optional ClamAV malware scanning can require a clean result before identity verification

The ClamAV check is disabled by default. When enabled, BookGuard streams the file bytes to a
deployment-configured `clamd` endpoint with the INSTREAM protocol; it does not give ClamAV a
host/library path. A malware detection, scanner error, timeout, missing scanner configuration, or
file above the configured scan limit fails closed: the file cannot be verified or admitted.
BookGuard does not send a file to ClamAV after an earlier deterministic safety check has already
failed, which avoids handing malformed or archive-bomb-like input to another parser.

Only a detection is evidence that a file is unsafe. When the scanner cannot finish (a timeout,
dropped connection, clamd error, or size limit), the library result is recorded as
`INSUFFICIENT_EVIDENCE` with source `malware-scan-inconclusive`, never as `UNSAFE_FILE`. It is
not reused from the verification cache, so the next verification scans the file again, and
Observe proposes re-verification rather than quarantine. `UNSAFE_FILE` results saved by earlier
releases for scanner failures are recognised and treated the same way.

The scan timeout defaults to 300 seconds (`BOOKGUARD_MALWARE_SCAN_TIMEOUT_SECONDS`, 1–300);
heavily illustrated EPUBs can take several minutes in clamd. The ClamAV overlay raises clamd's
100 MiB `StreamMaxLength`/`MaxFileSize` defaults to 512 MiB and `MaxScanSize` to 1024 MiB to
match `BOOKGUARD_MALWARE_MAX_BYTES`; override them with `BOOKGUARD_CLAMD_STREAM_MAX_LENGTH`,
`BOOKGUARD_CLAMD_MAX_FILE_SIZE` and `BOOKGUARD_CLAMD_MAX_SCAN_SIZE`. clamd reads them at start,
so recreate the scanner (`docker compose ... up -d`) after changing them.

For the optional private ClamAV topology, start BookGuard with the malware
overlay. The scanner has no library mounts and port 3310 is not published to
the host:

```bash
bash scripts/build-with-provenance.sh
docker compose -f compose.yaml -f compose.clamav.yaml up -d --no-build
```

On first start ClamAV may need time to initialize its signature database. Once
the containers are up, run the live acceptance test:

```bash
docker compose -f compose.yaml -f compose.clamav.yaml \
  exec -T bookguard python -m tools.clamav_acceptance
```

The acceptance test creates temporary clean and EICAR antivirus-test files
inside the BookGuard container, proves the clean control is accepted, proves
ClamAV detects EICAR, and proves BookGuard converts that detection into a
fail-closed security result. The temporary files are deleted automatically and
no test file is written to the ebook library, staging, or quarantine paths.

A deterministic failure produces an `UNSAFE_FILE` verdict before content identity is evaluated.
Unsafe files cannot pass staged verification or controlled admission. Plain-text and legacy formats
without a reliable fixed signature are reported as not applicable instead of being guessed.
An EPUB whose valid `mimetype` entry is merely compressed, not first, or padded with a UTF-8
BOM or ASCII whitespace receives a visible conformance warning rather than an unsafe verdict;
structural and identity verification continue.

Metadata repair settings include:

- repair mode: Off / Preview only / Safe repairs enabled
- audiobook and ebook repair enable/disable
- canonical normalization for confirmed PASS items
- set audiobook Album from Bindery title
- set audiobook Album Artist from Bindery author
- optional audiobook Genre and configurable genre value

Repair history and Undo are available at `/repairs`.


## Backup and validation

BookGuard's persistent application state lives in `/config/bookguard.db`. The
backup helper creates a transactionally consistent SQLite copy while the normal
BookGuard container may remain running. It does not copy media files, Bindery's
database, credentials, verification snapshots, staging, or quarantine content.

Create a backup on the Unraid host:

```bash
bash scripts/bookguard-backup.sh create
```

The default destination is:

```text
/mnt/cache/appdata/bookguard-backups
```

Or supply a different dedicated backup root:

```bash
bash scripts/bookguard-backup.sh create /mnt/user/backups/bookguard
```

Each backup is a directory containing `bookguard.db` plus a manifest with the
BookGuard version, database SHA-256, byte count, SQLite integrity result, and
table inventory. Creation uses the exact image and non-root UID:GID of the
existing BookGuard container, mounts production `/config` read-only, disables
networking, and gives the one-shot container write access only to the backup
destination.

Validate a backup without modifying it:

```bash
bash scripts/bookguard-backup.sh validate \
  /mnt/cache/appdata/bookguard-backups/bookguard-YYYYMMDDTHHMMSSZ
```

Validation mounts the backup read-only and checks the manifest, SHA-256, byte
count, SQLite integrity, and required BookGuard tables. Validation never
overwrites or restores production state.

To prove that a validated backup can be opened by the current BookGuard code
without touching production, run:

```bash
bash scripts/bookguard-backup.sh restore-validate \
  /mnt/cache/appdata/bookguard-backups/bookguard-YYYYMMDDTHHMMSSZ
```

Restore validation mounts the backup read-only, copies its database into a
disposable 512 MiB container tmpfs, and runs BookGuard's current startup
database initialization against that copy. The one-shot container has no
network, the image root remains read-only, and no production config or media
path is writable. The disposable restore copy disappears when the command
exits.

## Updating

Before deploying an update, keep BookGuard's writable `/config` mount in its
dedicated host directory rather than the Git checkout, and keep the required
credentials in the existing `.env`. To retain direct LAN access, set the bind
address and non-root runtime identity explicitly:

```env
BOOKGUARD_AUTH_USERNAME=bookguard
BOOKGUARD_AUTH_PASSWORD=replace-with-a-long-random-password
BOOKGUARD_BIND_ADDRESS=192.168.1.10
BOOKGUARD_UID=99
BOOKGUARD_GID=100
BOOKGUARD_CONFIG_HOST_PATH=/mnt/cache/appdata/bookguard-config
```

Use the guarded upgrade helper instead of combining an ad-hoc pull, build, and
Compose deployment.

A non-deploying preflight may be run on a synchronized feature or integration
branch:

```bash
bash scripts/safer-upgrade.sh preflight
```

Preflight refuses a dirty, detached, ahead, or behind checkout, runs the full
isolated smoke suite, builds with exact Git provenance, and verifies the
candidate image labels. It does not recreate the running production container.

The production upgrade command is intentionally restricted to `main` and
requires an exact confirmation token:

```bash
bash scripts/safer-upgrade.sh upgrade --confirm DEPLOY_BOOKGUARD_UPGRADE
```

Before deployment, the helper repeats preflight, creates a transactionally
consistent BookGuard database backup with the candidate image, validates that
backup read-only, proves restore compatibility in disposable tmpfs, rechecks
the source revision, and tags the currently running image for rollback. It then
deploys only `compose.yaml` plus `compose.clamav.yaml` with `--no-build`.

Post-deployment acceptance fails closed unless all of these succeed:

- the running container uses the exact candidate image
- OCI version/revision/source provenance matches the checked-out source
- Docker health and the `/health` version are correct
- BookGuard remains non-root with a read-only root filesystem, bounded no-exec
  `/tmp`, dropped capabilities, no-new-privileges, read-only media/Bindery
  mounts, and no writable action/admission aliases
- ClamAV remains private, has no published port or media/config mounts, and
  retains only its signature database volume
- the live clean/EICAR ClamAV acceptance test passes and BookGuard still fails
  closed on malware detection

If a post-deployment check fails, the helper does **not** automatically restore
the database or roll back the image. It preserves and prints both the rollback
image tag and validated backup so recovery remains an explicit operator action.

A deployed v0.6+ instance can be rechecked without redeploying:

```bash
bash scripts/safer-upgrade.sh verify
```

## Isolated smoke test

Before a release or live acceptance test, run:

```bash
./scripts/smoke-test.sh
```

The command builds the current source and validates the normal Compose topology,
each opt-in writable alias, and their combined topology. It then exercises the complete ebook
workflow—acquisition, external-handoff enforcement, staged-byte verification,
atomic admission, registration, and finalization—against a fake Bindery client
and temporary files.

The production Compose service runs BookGuard as a non-root UID/GID
(default `99:100` on Unraid), drops all Linux capabilities, and uses a read-only
root filesystem. Bindery's read-only database and BookGuard's writable host
directories therefore need normal filesystem ownership/permissions for that
runtime identity; BookGuard does not bypass DAC permissions with capabilities.

Writable state is limited to the explicit `/config`, `/staging`, and `/quarantine`
mounts plus any deliberately enabled action/admission alias. `/tmp` is a
bounded tmpfs mounted with `nosuid`, `nodev`, and `noexec`; application code,
Python packages, and system binaries remain immutable at runtime.

The workflow container uses `--network none`, a read-only root filesystem, and
the same bounded no-exec temporary filesystem. It does not mount `/config`, `/books`,
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
app/ebook_security.py       Deterministic file-signature and structure validation
app/verification_engine.py  Ebook identity classification and safety rules
app/verification_constants.py Shared verification limits
app/staging.py              Read-only staged-byte verification
app/admission.py            Atomic direct-admission transaction and recovery
app/acquisition.py          One-at-a-time Bindery queue-to-staging workflow
app/acquisition_coordinator.py Restart-safe supervised workflow advancement
app/acquisition_progress.py Supervised known-queue staging progression
app/observe.py              Non-mutating automation decision journal and policy
app/automatic.py            Guarded automatic-maintenance workflow
app/bindery_client.py       Bindery API and API-key discovery
app/file_safety.py          Shared filesystem hashing/safety helpers
app/no_replace.py           Atomic no-replace rename for publication and quarantine
app/media_evidence.py       Unified ebook/audiobook media evidence (E2)
app/audiobook_verification.py Audiobook identity and readability verification
app/recovery_classifier.py  Failure classification into recovery kinds
app/recovery_planner.py     Durable recovery plans and step definitions
app/automatic_execution.py  E4 execution journal, allowlist, and one-step executor
app/automatic_contracts.py  Shared E4 execution policy types
app/automatic_runner.py     Serialized E4 cycle dispatch and guarded finalization
app/acquisition_admission_*.py Verified-acquisition admission preflight, execution, and scan
app/admission_prepublication_review.py Proof for admissions that failed before publication
app/prepublication_retirement.py Retirement of one proven pre-publication failure
app/publication_*.py        Publication proof, recovery, and registration scan
app/registration_correction.py E4 registration-conflict correction executor
app/automatic_alternate.py  Operator-selected alternate grab executor
app/alternate_*.py          Alternate candidate review and durable operator selection
app/automatic_quarantine*.py Unsafe-media quarantine and post-quarantine grab
app/quarantine_*.py         Quarantine filesystem moves, selection, handoff, and final-state proof
app/catalogue_relationship.py How a misfiled ebook relates to the book it really is
app/catalogue_move.py       Guarded move of a misfiled ebook to the right book
app/attention.py            Attention queue assembly
app/home.py                 Read-only Home summary (health, decisions, activity)
app/activity.py             Activity timeline: plain sentences, who, result, filters
app/change_log.py           Records of setting changes and BookGuard starts/updates
app/import_watch.py         Checks each new Bindery import soon after it appears
app/health.py               System health checks with how-to-fix guidance
app/scheduler.py            Optional scheduled library scan and the task list
app/library_review.py       Open items grouped by the decision they need
app/attention_execution.py  Attention entries for interrupted E4 receipts
app/operator_guidance.py    Read-only operator guidance for Attention items
tools/smoke_test.py         Isolated workflow and Compose safety harness
tools/clamav_acceptance.py  Live ClamAV clean/EICAR acceptance test
scripts/smoke-test.sh       One-command containerized smoke-test runner
compose.clamav.yaml         Optional private ClamAV deployment overlay
templates/_layout.html      Shared header: Home, Review, Activity, System, Settings
static/home.js, review.js   Home and Review page controllers
static/triage-acquisition.js Supervised replacement UI controller
static/triage-move.js       Move-to-correct-book UI controller
```

Versioned entry-point and verifier wrappers are intentionally avoided. New
behavior should be added to the relevant router or service and covered by a
focused test instead of layering another `main_v*` or `verifier_v*` module.
