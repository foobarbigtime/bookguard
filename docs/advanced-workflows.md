# Advanced workflows

Start with the [everyday user guide](user-guide.md). This reference describes
misfiled ebooks, import checks, scheduling, Observe mode, and shared-file
conflicts.

## Contents

- [Move a misfiled ebook](#moving-a-misfiled-ebook-to-the-right-book)
- [Check new imports](#checking-new-imports)
- [Schedule a library scan](#scheduled-scan)
- [Observe mode](#observe-mode-v06-foundation)
- [Shared-file registration conflicts](#shared-file-registration-conflicts)

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

Supported values are `manual` and `observe` (`automatic` is still accepted but
now behaves like `manual`); any other value is a configuration error. `manual` preserves the explicitly confirmed workflow.
`observe` records durable decisions about recovery work using existing
BookGuard database state, but does not invoke Bindery mutation, queue mutation,
staging cleanup, quarantine, metadata writes, or library publication.

Observe decisions are idempotently journaled in `automation_observations`,
appear in unified History, and the latest decision for a subject appears in
Attention when the decision is `attention`. Missing durable ebook verification
is recorded as `would_verify_result` rather than Attention because verification
is a read-only prerequisite.
Observe Mode itself still does not open the library file. A later safe/terminal
state records a resolving `no_action` decision so stale Observe Mode attention
does not remain active.

```text
GET  /api/automatic/observe
POST /api/automatic/observe/run
```

The run endpoint requires the exact confirmation token
`RUN_OBSERVE_MODE`. Running it may write only BookGuard's own durable decision
journal; the source scan, verification, repair, cleanup, Bindery, queue,
quarantine, and library state are not changed by the Observe decision engine.

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

## Replacing a bad file

BookGuard no longer downloads, stages or publishes replacement files itself.
**Replace** quarantines the bad file (Put back can undo it), blocklists the
release it came from in Bindery, keeps the book monitored and runs Bindery's
Automatic search. Bindery picks, downloads and imports a new copy with your own
Bindery settings, and BookGuard checks it like any other import.

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
No writable library alias is
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
`compose.actions.yaml` writable alias; `/books` remains read-only.

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
