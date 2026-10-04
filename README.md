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

It answers two related questions:

> Does the file Bindery imported actually look like the book Bindery thinks it is?

> If the file is the correct book but its metadata is broken, can that metadata be repaired safely?

BookGuard is designed around **audit first, repair second, destructive actions last**.

## Downloads are Bindery's job

Earlier versions could find, grab, stage and publish a replacement ebook
themselves (automatic reacquisition, the supervised coordinator, direct
admission and Supervised Automatic Mode). That code is gone: **Replace**
quarantines the bad file and hands the rest to Bindery, which searches,
downloads and imports with your own Bindery settings. BookGuard checks the new
import like any other. Old records of those workflows stay in Activity.

Still here from v0.6:

- **Observe Mode** records what BookGuard would do about a problem, without
  changing anything.
- **Unified media evidence** verifies audiobooks as well as ebooks, and detects
  ebook/audio cross-assignment from the actual bytes and containers.
- **Attention** on Home collects items that need you, with read-only guidance.

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
- Can put a file quarantined from Triage back where it was: open the quarantine in Activity and choose **Put back**. BookGuard refuses if anything is now at the original path, the quarantined file is missing or changed, or the path is outside the library folders. It then gets Bindery to track the file on the same book again (a library scan, adopting the unmatched file to that book, or, if the scan attached it to the wrong book, Bindery's Fix match) and turns monitoring back on. It also refuses if Bindery already has another copy of the book. Quarantines made before this version did not record enough to be put back.
- **Replace** (the main button on a Review book that contains the wrong file) quarantines a bad file and hands the rest to Bindery: it blocklists the download the file came from, keeps the book monitored, and runs Bindery's Automatic search, so Bindery picks, downloads and imports a new copy with your Bindery settings. BookGuard checks the new import like any other. If Bindery's automatic grabbing is off, Activity says so and what to click. Replace refuses when Bindery already has another copy of the book; use Quarantine then.
- **Review → Duplicates** lists books Bindery shows twice for the same author because the titles differ only in case, punctuation or a leading article ("Nightingale" and "The Nightingale"). **Fix now** hides the empty extra entries BookGuard can prove are duplicates of a book you have, with Bindery's own exclude (kept, never searched for again, and can be included again on the book's page); when no entry has files, it keeps one. To do this every hour, turn on **Fix duplicate Bindery entries every hour** under Settings → Schedule (off by default, and only while actions are on). Entries with files are never changed: an ebook and audiobook on two entries, and two copies of the same kind, are only listed. Proof means the titles and author agree, Bindery's series data doesn't contradict it, and Bindery's year and language labels don't disagree, unless a shared ISBN/ASIN or series place proves it anyway; otherwise the pair is left for you with the reason. Anything the last run left alone is listed at the top of the page. Every change is checked again right before, recorded in Activity, and nothing is deleted.
- **Review → Unmatched** checks the files Bindery's library scan could not place (Bindery's Import page) and gives each a plain verdict with the evidence: **Junk** (not the file type it claims, broken, DRM-locked, nearly empty, a sample, silent audio, unreadable audio, or an exact copy of a file the library has), **Not in your library** (a real book whose own metadata/tags/ISBN and names agree, by an author or title Bindery doesn't have), **Belongs to a book** (at least two independent sources, one of them the file's own metadata, tags or ISBN, name the same Bindery book and it has no file of that kind yet; **Attach** uses Bindery's own adopt, which Bindery can undo), **Extra copy** (that book already has a file of this kind, so there is no Attach), **Another-language edition** (a translation of a library book), or **Unsure** (including when a file names a pen name or narrator for a book your library has under the folder's author). Titles match through series and number prefixes ("05 - ", "Book 4 - ", "TDT 0.5 "), ": Series, Book N" subtitles and "(Unabridged)"; authors match in any name order ("Rowling J.K.", "Corey, James S.A."); folders like "eBook", "m4b", "US" or "Edition 1" are skipped for the book folder above them; audio tagged with a music genre is **Junk**. It says **Unsure**, with the reason, when your library lists the book more than once, when only the audio tags name an author the folder disagrees with (often the narrator), or when the title is a series name and number ("Murderbot Diaries 05") of one of the author's Bindery series and Bindery doesn't say which book has that place. When it does, that book is the match. Books you excluded in Bindery are left out, as Bindery hides them too. Bindery's look-alike title suggestions are never taken as proof. Click **Check now** to run it; it only reads files.
- Each Review book shows the one action that fits its problem (Replace, Quarantine, Move or Fix details); the rest is under **More**. "Waiting for you" entries in Activity clear themselves once Bindery shows the problem is fixed.
- Quarantine from Triage stops Bindery monitoring the book first, so Bindery does not download a replacement while the file is out. If the quarantine fails, monitoring is turned back on.

BookGuard **never automatically deletes media**.

## How files are checked safely

- Default-on archive safety checks before EPUB/CBZ extraction.
- Ebook verification opens sources with no-follow semantics, snapshots stable bytes into private /config storage, runs every parser and ClamAV against that same snapshot, then re-checks the original path before accepting the verdict.
- SHA-256 and file-stat stability checks across verification.
- The normal BookGuard container uses a read-only root filesystem; only explicit data mounts and a bounded no-exec /tmp tmpfs are writable.
- Optional fail-closed ClamAV malware scanning through a deployment-only clamd endpoint.
- Quarantine is a move, never a delete: cross-mount copies are verified before a source is removed, and Put back restores the exact bytes.

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
and reconciliation requests also require an explicit
operation-specific confirmation in the JSON body. Authentication and
confirmation serve different purposes and both are enforced by the server.

### Bindery actions

```env
BOOKGUARD_ALLOW_ACTIONS=false
```

This controls every media-changing operation, including Detach, Quarantine,
Replace and Put back. `PASS` results remain protected
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

To switch ebook quarantine on, set `BOOKGUARD_EBOOK_ACTIONS_ENABLED=true` in
`.env` and run the usual upgrade command. `scripts/safer-upgrade.sh` then adds
`compose.actions.yaml` (the writable alias) by itself, on this and every later
upgrade, and its hardening check accepts the alias only while ebook actions are
on and the alias is the same host folder as the read-only `/books` mount.
System → Health says whether Quarantine, Replace and Put back are ready, and if
not, the one thing to do. The read-only source path and writable alias must have
identical library-relative paths and must resolve to the same device and inode
before BookGuard will move anything. A failed move occurs before Bindery is
changed; a later detach failure causes BookGuard to restore the file through the
writable alias.

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
The time follows daylight saving. If BookGuard was stopped, or the scan can't
start (for example Bindery's database can't be read), it keeps trying for up to
three hours after the set time; a scan that still didn't start is reported in
Health until a scan runs.
**System** lists every scheduled task with its last and next run and a
**Run now** button, and its Health section explains any problem and how to fix
it.

### Observe Mode (v0.6 foundation)

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
Unsafe files are never treated as the right book. Plain-text and legacy formats
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

The command builds the current source, checks that the image runs with a
read-only root filesystem, and validates the normal Compose topology, the
writable action alias overlay, and the ClamAV overlay.

The production Compose service runs BookGuard as a non-root UID/GID
(default `99:100` on Unraid), drops all Linux capabilities, and uses a read-only
root filesystem. Bindery's read-only database and BookGuard's writable host
directories therefore need normal filesystem ownership/permissions for that
runtime identity; BookGuard does not bypass DAC permissions with capabilities.

Writable state is limited to the explicit `/config`, `/staging`, and `/quarantine`
mounts plus the action alias when deliberately enabled. `/tmp` is a
bounded tmpfs mounted with `nosuid`, `nodev`, and `noexec`; application code,
Python packages, and system binaries remain immutable at runtime.

The smoke containers use `--network none` and mount no live host path. A live
test is still appropriate once per release milestone.

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
repair, verification, repair-history persistence and Undo.

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
app/observe.py              Non-mutating automation decision journal and policy
app/bindery_client.py       Bindery API and API-key discovery
app/file_safety.py          Shared filesystem hashing/safety helpers
app/no_replace.py           Atomic no-replace rename for publication and quarantine
app/media_evidence.py       Unified ebook/audiobook media evidence (E2)
app/audiobook_verification.py Audiobook identity and readability verification
app/recovery_classifier.py  Failure classification into recovery kinds
app/recovery_planner.py     Durable recovery plans and step definitions
app/quarantine_fs.py        Quarantine filesystem moves with verified cross-mount copies
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
app/attention_execution.py  Attention entries for interrupted automatic steps from older versions
app/operator_guidance.py    Read-only operator guidance for Attention items
tools/smoke_test.py         Isolated root-filesystem and Compose safety checks
tools/clamav_acceptance.py  Live ClamAV clean/EICAR acceptance test
scripts/smoke-test.sh       One-command containerized smoke-test runner
compose.clamav.yaml         Optional private ClamAV deployment overlay
templates/_layout.html      Shared header: Home, Review, Activity, System, Settings
static/home.js, review.js   Home and Review page controllers
static/triage-move.js       Move-to-correct-book UI controller
```

Versioned entry-point and verifier wrappers are intentionally avoided. New
behavior should be added to the relevant router or service and covered by a
focused test instead of layering another `main_v*` or `verifier_v*` module.
