# UI redesign: workflow first

Status: design approved (mockups). Build steps 1-5 (Foundations, Home, Review,
Activity, System) are implemented; steps 6-7 are not yet.
Mockups: <https://claude.ai/artifact/XAaZbvzFmJVCTgo8ZeX3Tp> (Home, Review,
Replace, Setup, Activity, System, Settings).

For current operator instructions, use the [user guide](../user-guide.md).
Put back, the simple Review Replace action, Unmatched, Duplicates, and Review
pagination have also shipped. Step 7 still refers to the proposed guided
supervised replacement/setup flow, not the simple Replace action already on Review.

## Goal

> Open BookGuard, immediately understand what is happening, deal with anything
> that needs attention, and leave.

The operator is self-hosted and may open BookGuard only every few weeks. The
first screen must be understandable without remembering how BookGuard is built
internally. The interface follows the operator's workflow, not the
application's module structure.

The redesign changes **presentation only**. It does not remove or weaken any
backend safety check, confirmation, diagnostic, audit record, or recovery path.

## Principles

1. **Answer five questions on the first screen:** Is BookGuard healthy? Is
   anything waiting for me? What is it doing right now? Is automation on, and
   what may it do? What happened recently?
2. **Progressive disclosure:** simple status first, useful detail when
   expanded, deep diagnostics only on request.
3. **Plain language:** internal terms (E4, execution plans, admission
   primitives, publication recovery, readiness internals) do not appear in
   primary UI. They remain available under System → Advanced and in
   technical-detail panels.
4. **Group decisions, not rows:** Home shows *"23 books contain the wrong
   file"*, not 23 rows. The individual items are one click away.
5. **Every attention item says:** what is wrong, why, what BookGuard already
   did, and what the operator can do next.
6. **Destructive and high-risk actions are separated** (red, set apart,
   confirmation dialog) and say plainly when nothing is deleted.
7. **No page exists only because a module or endpoint exists.**
8. **Visual language follows Bindery:** zinc-950 background, zinc-900 cards,
   zinc-800 borders, emerald accent, system fonts, rounded cards. Restyled in
   CSS over the existing Jinja2 templates; no framework change.

## Navigation

Seven pages become four plus a gear, arr-style:

```
Home  ·  Review  ·  Activity  ·  System                ⚙ Settings
```

| New | Replaces | Purpose |
|---|---|---|
| **Home** | Dashboard, Attention | The five questions; grouped attention with next actions |
| **Review** | Triage, Repairs | Books needing a decision; per-book detail and actions |
| **Activity** | History, history detail, automation journal | One plain-language timeline |
| **System** | Diagnostics | Health, scheduled tasks, backups, advanced diagnostics |
| **Settings** (gear) | Settings | All configuration, with *Show advanced* |

## Inventory of the current UI

This is the planning inventory from before the redesign. The implementation
notes below and the [user guide](../user-guide.md) describe the current pages.

Legend: **Main** = main-page essential · **Detail** = secondary detail ·
**Advanced** = advanced diagnostic · **Remove** = redundant as a separate place.

| Current location | Element | Category | New home |
|---|---|---|---|
| Dashboard | Status cards (Bindery actions, repair mode, Bindery DB, latest scan) | Main | Home health line and System |
| Dashboard | Scan library, Cancel safely | Main | Home / System tasks |
| Dashboard | Stop immediately | Detail | Scan "More" menu |
| Dashboard | Detach all confirmed missing | Detail, destructive | Review, danger zone |
| Dashboard | REVIEW chips, results table, per-row repair/detach/quarantine | Detail | Review |
| Dashboard | Cleanup history | Remove | Activity |
| Attention | Attention queue with guidance | Main | Home |
| Triage | Review/reject table, Verify, Verify all, Mark reviewed, Reopen | Detail | Review |
| Triage | Verification progress | Main | Home "Right now" |
| Triage | Supervised replacement panel | Detail | "Start a replacement" flow |
| Triage | Coordinator/staging/queue tiles | Advanced | System → Advanced |
| Triage | Shared-file conflicts tool | Detail | Review tools; interruptions surface on Home |
| Triage | Triage action history | Remove | Activity |
| History + detail | Ten record kinds; stored fields and raw evidence | Detail / Advanced | Activity (expandable technical details) |
| Repairs | Candidates, history, Undo | Remove as a page | Review (candidates), Activity (Undo on the entry) |
| Diagnostics | Deployment environment and gate states | Advanced | System → Advanced |
| Diagnostics | ClamAV and Bindery status | Main (summary) | Home health, System health |
| Settings | All sections, Test Tika, Test scanner, Reset | Detail | Settings (tests under Advanced; Reset in a danger zone) |
| API only today | Automation mode, allowlist, Observe run, automatic cycle | Main | Home automation card, Settings → Automation |
| API only today | Automatic run journal, readiness checks | Detail / Advanced | Activity, System → Advanced |

## Screens

### Home
- **Health line:** *"BookGuard is healthy · Nothing is blocking normal
  operation · N things need your decision"*, amber or red with the reason when
  not.
- **Library check strip:** verified / wrong file / damaged / undecided counts.
- **Needs your attention:** grouped items (for example adult content filed
  under other books, wrong files, Bindery entries that are not real books,
  audiobook folders naming several books, books to re-check after a pen-name
  change, stalled replacements), each with why, what BookGuard did, and
  actions.
- **Right now:** current job with progress, current item, and time left;
  next scheduled task.
- **Automation:** Manual / Observe / Automatic switch, one sentence on what it
  may do, last check, Run observe check, Pause automation.
- **System health:** BookGuard version, virus scanner, Bindery, replacement
  readiness, last backup.
- **Recent activity:** totals (added, quarantined, blocked) plus the last few
  events.

### Review
- Filter chips mirroring Home groups, then the larger buckets (undecided,
  damaged, verified).
- A secondary switch inside a group (for wrong files: *Ready to replace* /
  *Check the Bindery entry*).
- Rows read as sentences: book, what the file actually contains, suggestion.
- **Detail panel** per book: what's wrong (one sentence), how BookGuard knows
  (plain evidence), the suggestion and why, primary actions, a separated
  danger zone (*Quarantine only…*, nothing deleted), and collapsed history and
  technical details.
- When ISBN evidence identifies the file as another catalogued book, the panel
  says so and distinguishes *duplicate catalogue entry* from *wrong book*.

### Start a replacement
- Five-step progress: remove the wrong file → choose a release → download and
  check → add to library → tidy up.
- Choosing a release: good matches selectable with plain reasons; unsuitable
  results listed with why they were rejected.
- Reassurance at commitment: nothing reaches the library until it is checked
  and approved.
- "Ready to download" checklist in plain words; "What happens next".

### Replacement setup (one time)
- Progress (*4 of 7 ready*); groups ordered by effort: already in place, needs
  your OK, needs a one-time server change.
- Bindery hand-off choice: **only while a replacement is running
  (recommended)** or always.
- The server change shown with copy-ready commands and **Check again**.
- *Continue* disabled until everything is ready; side panel: what changes, what
  if something goes wrong, can I undo this.

### Activity
- One timeline, grouped by day; each event a plain sentence with **who** (You,
  BookGuard, Automatic, System) and **result** (Done, All checks passed,
  Waiting for you, Interrupted).
- Filters: what, who, result (stopped or failed), when; search; totals.
- Expandable events (for example an update's six post-deploy checks, backup
  name, how to roll back); deeper technical detail one more click away.
- Actions live on their entry: Undo on a metadata repair, Put back on a
  quarantined file.

### System
- Tabs: **Status · Backups · Advanced**.
- **Health** messages, arr-style: what is wrong, why it matters, *How to fix*;
  a compact all-clear list below.
- **Scheduled tasks:** schedule, last run with duration, next run, *Run now*.
- **About:** version and commit, last update and result, uptime, Bindery and
  ClamAV versions, scanner limits, automation mode.
- **Advanced:** readiness checks, deployment and permissions, automation policy
  and allowed actions, automatic run journal, recovery plans, connection tests,
  diagnostics bundle.

### Settings
- Sidebar sections: General, Automation, Library checks, Virus scanning,
  Replacements, Metadata repair, Pen names, Bindery connection, Login and
  security. Status hints in the sidebar.
- *Show advanced* toggle; a save bar for unsaved changes; every saved change is
  recorded in Activity.
- **Automation:** mode cards (Manual / Observe / Automatic) with one sentence
  each; allowed actions as grouped checkboxes with plain descriptions and a tag
  (*Changes your library*, *Downloads*, *Bindery only*, *Bookkeeping*); a
  schedule and **quiet hours**; a locked list of what is never automatic
  (deleting files, editing Bindery's catalogue, choosing which release to
  download).

## Automation control model

Agreed: **arr-style**. Automation mode and the allowed actions are ordinary
settings, saved in BookGuard's database and changed from Settings, like the
existing Bindery actions switch. `.env` provides the initial default only.

- Switching to Automatic shows exactly what will be allowed and asks for
  confirmation.
- **Pause automation** is always one click on Home and takes effect
  immediately.
- Every change is recorded in Activity (when, old value, new value).
- Every automatic action still runs through E4's guards (allowlist, fresh
  boundary revalidation, one external mutation per cycle). Settings decide
  *whether* to act, never *how safely*.
- Trade-off, stated to the operator: the BookGuard login becomes the key to
  automation, as with the arr apps. Use a strong password, and HTTPS or a VPN
  beyond the LAN.

## Plain-language vocabulary

| Internal term | Shown as |
|---|---|
| ClamAV | Virus scanner |
| Verification job | Checking library files |
| `INSUFFICIENT_EVIDENCE` | Undecided / couldn't prove |
| `WRONG_CONTENT` | Contains the wrong file |
| `UNSAFE_FILE` | Damaged (or: virus found) |
| `METADATA_ERROR` | Right book, wrong details |
| Admission | Add to library |
| Acquisition / grab | Download |
| Staging | Download folder |
| External import readiness | Replacements set up |
| Observe cycle | Observe check |
| E4 automatic cycle | Automatic run |
| Recovery plan | (Advanced only) |
| Quarantine | Quarantine (moves the file aside; nothing is deleted) |

## Backend work the screens depend on

| Need | For |
|---|---|
| Automation mode and allowlist as persisted settings (env as initial default) | Home automation card, Settings → Automation |
| Pause automation | Home |
| Scheduler for tasks (scan, check new books, observe, automatic run, backup, snapshot cleanup) with quiet hours | System tasks, Settings → Automation |
| Setting-change log | Activity |
| BookGuard update events (detect revision change at startup; attach upgrade results) | Activity, System |
| Unexpected-restart detection (a job running at shutdown that never finished) | Activity, System health |
| Health checks: virus-definition age, nightly restarts, unpinned Bindery | System health |
| Temporary Bindery hand-off: switch import mode only while a replacement runs | Setup, Replace |
| `safer-upgrade.sh` aware of enabled write-path overlays (deploy and verify them instead of rejecting) | Setup |
| Alias changes invalidate the affected cached verdicts | Review (pen names) |
| Grouping of attention items by decision type | Home, Review |
| Summary endpoint for Home (health, counts, current job, automation, recent events) | Home |

## Build plan

Each step is a separate, reviewable PR.

1. **Foundations:** shared layout and navigation (four pages plus gear), the
   Bindery-style stylesheet, and redirects from old URLs.
2. **Home**, backed by a summary endpoint (read-only).
3. **Review**, merging Triage and Repairs; per-book detail panel.
4. **Activity**, unifying history and the automation journal; setting-change
   and update events.
5. **System**, with health checks and Advanced; the scheduler and tasks.
6. **Settings** with Automation as persisted settings, Pause, and quiet hours.
7. **Replacement flow and setup**, including the temporary Bindery hand-off and
   the upgrade-script changes.

Old pages stay reachable until their replacement ships. The audit trail and
every existing API remain unchanged.

Steps 1-3 as built (one PR):

- Every page shares one header (`templates/_layout.html`): Home · Review ·
  Activity · System and the Settings gear.
- **Home** (`/`) is backed by `GET /api/home` (`app/home.py`), read-only and
  without calling Bindery. Open REVIEW and REJECT items are grouped by the
  decision they need (`app/library_review.py`), alongside interrupted or
  refused workflows from the attention queue, each with what BookGuard did.
  The automation card shows the mode read-only until step 6 makes it a
  setting; "Right now" has no schedule until step 5.
- **Review** (`/review`) lists those items as sentences with a detail panel:
  what's wrong, how BookGuard knows, the suggestion and its actions (move,
  metadata fix, mark reviewed, verify again), a separate danger zone (detach,
  quarantine) and technical details. It uses the existing guarded endpoints
  and confirmations.
- Review now shares one workspace across books needing decisions, unassigned
  files, and duplicate entries. The six legacy tabs are removed. Advanced
  exposes reviewed books, repair previews/history, the detailed review table,
  and raw scan results. Triage bookmarks redirect to Review with query filters
  preserved. Bulk mark-reviewed and shared-file checks also live on Review.
- **Check library** on Home and Review runs the scan, unresolved REVIEW/REJECT
  verification, and unassigned-file check in order. It reuses compatible proof,
  reports postponed books, and reserves the sequence against overlapping job
  starts. Scheduled scans remain scan-only.
- Mixed recording folders require a separate recording-group preview/apply
  workflow. This change does not automate splitting or moving those recordings;
  Review avoids making whole-folder replacement their primary action.
- Old URLs redirect (307, query kept): `/triage` → `/review/triage`,
  `/attention` → `/`, `/?classification=…` → `/review/scan-results?…`,
  `/history` → `/activity`, `/diagnostics` → `/system`.

Step 4 as built:

- **Activity** (`/activity`, `GET /api/activity`, `app/activity.py`) is one
  timeline of every durable record: library scans, verifications (folded into
  one "Checked N library files" event per day), decisions (review, detach,
  quarantine, metadata fixes, moves, shared-file corrections), replacements,
  Observe checks (folded per day), automatic steps and recovery plans,
  setting changes and BookGuard starts. Each event is a sentence with who
  (You, BookGuard, Automatic, System) and how it ended (Done, Failed,
  Interrupted, Waiting for you, In progress), grouped under local day
  headings, with filters for what, who, result (or "problems only"), period
  and text, and the unchanged technical detail one click away.
- New records (`app/change_log.py`): setting saves and resets store exactly
  which values changed (the API key only as "changed"); every start stores
  the version and Git revision, so a start on a new revision reads as
  "BookGuard was updated to …".
- Undo for an applied metadata fix is on its Activity entry. **Put back** is
  now implemented for eligible Triage/Review quarantines, using their recorded
  paths and hashes. It checks the destination and bytes, restores the file,
  and follows up with Bindery association and monitoring. It is not a general
  undo for every automatic quarantine or replacement workflow. See
  [Undo and Put back](../user-guide.md#undo-and-put-back).
- Home's recent activity uses the same sentences. The Repairs tab stays
  until its repair proposals have a home in Review.

Step 5 as built:

- **System** (`/system`) has a Status tab and an Advanced tab (the previous
  diagnostics page, unchanged, at `/system/advanced`).
- **Health** (`app/health.py`, `GET /api/system/health`): each problem says what
  is wrong, why it matters and how to fix it; passing checks are a short list.
  Checks: Bindery's database readable, library folders readable, virus scanner
  answering and its definitions under three days old, BookGuard's database
  integrity (System only; Home skips it), a scan left running by a restart,
  repeated nightly restarts (from the start records, shown in local time),
  import checks failing, a scheduled scan that couldn't start,
  running as root. Home's health line uses the same checks.
- **Scheduled tasks** (`app/scheduler.py`, `GET /api/system/tasks`): an optional
  scheduled library scan (off, daily or weekly at a set time, container local
  time, following daylight saving; a missed slot runs if under three hours
  late, and a start that fails is retried in that window, then reported in
  Health until a scan runs), the import check and the
  Observe check, each with last and next run and Run now.
- **About**: version and revision, running since, last update, virus scanner,
  automation mode.
- Not built: in-app backups. Backups stay on the host
  (`scripts/bookguard-backup.sh`, and before every upgrade) so BookGuard's own
  data never needs a writable backup mount inside the container.
