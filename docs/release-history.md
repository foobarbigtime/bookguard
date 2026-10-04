# Release history

These notes describe the v0.5 and v0.6 foundations. They are historical context;
the [user guide](user-guide.md), [configuration](configuration.md), and
[workflow reference](advanced-workflows.md) describe current behavior.

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

See [Observe Mode](advanced-workflows.md#observe-mode-v06-foundation) and
[Supervised Automatic Mode (E4)](advanced-workflows.md#supervised-automatic-mode-e4) for
configuration and endpoints.

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
