# Advanced workflows

Start with the [everyday user guide](user-guide.md). This reference describes
misfiled ebooks, import checks, scheduling, shared-file conflicts, and the
separately gated staging and recovery workflows.

## Contents

- [Move a misfiled ebook](#moving-a-misfiled-ebook-to-the-right-book)
- [Check new imports](#checking-new-imports)
- [Schedule a library scan](#scheduled-scan)
- [Observe mode](#observe-mode-v06-foundation)
- [Automatic reacquisition](#automatic-reacquisition)
- [Supervised Automatic mode](#supervised-automatic-mode-e4)
- [Controlled ebook admission](#controlled-ebook-admission)
- [Shared-file registration conflicts](#shared-file-registration-conflicts)
- [Automatic maintenance API](#automatic-maintenance-api)

## Moving a misfiled ebook to the right book

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

## Checking new imports

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

## Scheduled scan

A full library scan can run on a schedule: **Settings → Scanning →
Scheduled library scan** (off, daily or weekly, at a set time in the
container's local time; set `TZ` on the container to use yours), or
`BOOKGUARD_SCAN_SCHEDULE`, `BOOKGUARD_SCAN_SCHEDULE_TIME` and
`BOOKGUARD_SCAN_SCHEDULE_DAY` (0 = Monday … 6 = Sunday). It is off by default.
The time follows daylight saving. If BookGuard was stopped, or the scan can't
start (for example Bindery's database can't be read), it keeps trying for up to
three hours after the set time; a scan that still didn't start is reported in
Health until a scan runs.
**System** lists every scheduled task with its last and next run and a
**Run now** button, and its Health section explains any problem and how to fix
it.

## Observe Mode (v0.6 foundation)

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

### Unified media evidence (v0.6 E2)

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

## Automatic reacquisition

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

## Supervised Automatic Mode (E4)

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

## Controlled ebook admission

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

## Shared-file registration conflicts

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

## Automatic maintenance API

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

See the [safety model](safety.md) before enabling a writable alias.
