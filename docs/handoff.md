# BookGuard development handoff

Checkpoint: 2026-10-12. This document is for Claude and other coding assistants
continuing the project. Check GitHub for current heads before resuming.

## Branch and scope

- Main reviewed baseline: `45a12e00ef1b83543a043043a4682b8902bd3d24`.
- OCR dependency: [PR #161](https://github.com/foobarbigtime/bookguard/pull/161),
  branch `claude/relaxed-pasteur-gso7yq`, reviewed at
  `d2977e9b975166c4011777404cc92b1741affa27`.
- This follow-up: [PR #162](https://github.com/foobarbigtime/bookguard/pull/162),
  branch `codex/verification-hardening-handoff`.
  It is based on that OCR head and targets Claude's OCR branch, preserving the
  existing PR. Merge the follow-up into the OCR branch before #161 goes to main,
  or move the follow-up to main if #161 has already merged. Re-run CI after
  changing the base or combining changes.
- The user's latest request authorizes continuing implementation and keeping
  documentation accurate for a Claude handoff. GitHub is the durable source
  of truth; a local scratch checkout is not the handoff.

## Completed in this follow-up

1. Native ebook verification no longer marks a file correct from matching
   metadata plus a shared author's name when the title is absent. The same
   requirement covers the second evidence pass and download provenance.
   Assigned or matching file title text supplies content support; corroborated
   ISBN evidence remains a separate supported path.
2. Old author-only correct verdicts are rechecked. Second-pass version is 4;
   older second-pass verdicts are re-examined. The base verifier version stays
   28 so unrelated cache keys remain compatible.
3. Actual OCR attempts capture their effective policy before rendering and
   record the settings actually used, even if Settings changes during the run.
   Page/language changes
   refresh affected PDFs; disabling OCR refreshes OCR-derived verdicts.
   Tool/render/read failures are retried on the next verification. Completed
   no-text attempts stay cached until bytes or policy change.
4. Compose and `.env.example` forward all three OCR settings. Languages remain
   deployment-only, including persisted-setting exclusion. Saved UI on/off
   and page values still override environment defaults.
5. CI installs Poppler, Tesseract and Pillow and asserts their availability
   before pytest, so real scanned-PDF tests run instead of silently skipping.
6. README now uses Check library. Configuration, architecture, safety,
   development and user guides describe the implemented rules. Absolute
   claims that OCR cannot misidentify text were replaced with the actual
   exact/near-match policy.

## Validation checkpoint

Local checks already run: Python compilation, `git diff --check`, and all 17
plain verification-engine test functions executed directly, including the new
same-author regressions and retained title/ISBN paths. Full and initial author
credits in the reproduced wrong-book case remain `INSUFFICIENT_EVIDENCE`.
Three disposable scanned PDFs were exercised through actual extraction and
classification on this follow-up: a correct title page verified at 99, a wrong
book by the same author stayed undecided, and placeholder metadata produced a
97-confidence metadata-error verdict. A real blank scan returned `no_text`.

Local pytest and Ruff dependencies are unavailable in this execution
environment. Do not report the full local suite as passed.

Fully validated code checkpoint:
`a021f6a5589240346758e79011e0ba9b43cf37b7`, from
[CI run 38313275982](https://github.com/foobarbigtime/bookguard/actions/runs/38313275982).
The run completed successfully on 2026-10-12:

- 1,004 tests passed, zero skipped; all six real scanned-PDF/OCR cases ran.
- Ruff, shell syntax checks, and both acceptance-harness self-tests passed.
- Isolated container smoke and quarantine copy-failure acceptance passed.
- One existing FastAPI/Starlette TestClient deprecation warning was reported.

The host-tested revision `4f5e4ef0b9ccb0cd0938e5691f96bc1a578caf47` also
passed its full [CI run 38313699688](https://github.com/foobarbigtime/bookguard/actions/runs/38313699688).
Later documentation-only commits record this evidence without changing the
tested code. Before merging, check the current PR head's CI instead of assuming
this dated checkpoint is still the latest result. #162 is prepared for Claude's
independent review; it and #161 remain unmerged at this checkpoint.

Prior baseline evidence, not validation of this follow-up:

- Main `45a12e0`: 963 tests passed, plus lint, harness self-tests, container smoke
  and quarantine copy-failure acceptance.
- OCR PR `d2977e9`: 982 passed and 6 real-OCR tests skipped in its original CI.
- Three disposable real OCR checks on the reviewed OCR baseline recognized a
  correct scanned book, left a wrong book by the same author undecided, and
  recognized a title page carrying placeholder metadata.

## Unraid acceptance checkpoint (2026-10-12)

Les reported these results from the Unraid console:

- Production checkout `/mnt/cache/appdata/bookguard` is clean on `main`.
  The checkout and running image revision both match
  `45a12e00ef1b83543a043043a4682b8902bd3d24`.
- `bookguard` and `bookguard-clamav` are healthy. The application's
  `/health` returned `{"status":"ok","version":"0.6.0"}`.
  Les clicked Check library on this deployed version during host acceptance.
  Its completion and active scan/verification job status have not been checked.
- The detached acceptance worktree is
  `/mnt/cache/appdata/bookguard-pr162-acceptance`, pinned to
  `4f5e4ef0b9ccb0cd0938e5691f96bc1a578caf47`.
  Keep host tests pinned to this revision while later documentation-only
  commits record their results.
- The isolated smoke test passed using image
  `bookguard-pr162-smoke:local`. Its terminal result was
  `PASS: BookGuard smoke tests completed without mounting live data or using a network.`
- The pinned `quarantine-copy-fault` host scenario also passed using
  `bookguard-pr162-smoke:local` (image ID
  `sha256:4009d44ec4cb12f8b84900898d1fa5ce72132ce0f605ef99d344be759913029a`).
  Its injected corrupt private copy was refused; the exact source and
  unrelated control bytes survived. Terminal result:
  `PASS: quarantine-copy-fault`.
- OCR executable availability passed in the same smoke image, running as
  UID/GID 99:100 with no network, a read-only root filesystem, all capabilities
  dropped and no-new-privileges. The tools resolved to `/usr/bin/pdftoppm`,
  `/usr/bin/tesseract` and `/usr/bin/prlimit`; Tesseract listed `eng` and `osd`.
  This confirms installed tools/languages; representative library PDF verdicts
  still need inspection after a reviewed production upgrade.
- Docker's `/var/lib/docker` is on a 60 GB loop filesystem. After reclaiming
  1.007 GB of unused build cache and running both host tests, it still reported
  4.2 GB available and 94% used. Recheck space before further builds.

All three planned host checks (isolated smoke, quarantine-copy-fault and OCR
executable/language availability) passed at the pinned revision. Independent
Claude review, merges and a guarded production upgrade remain pending. No
production deployment has been performed in this session.

## Next steps for Claude

1. Independently review the follow-up and its regression coverage, especially
   native/initial/provenance author-only cases, ISBN compatibility, and OCR
   policy/error cache transitions. Check the exact PR head's complete CI and
   confirm the real OCR cases ran without skips.
2. Combine the follow-up with #161 in the order above; use the combined CI
   result and record the merged main revision here. Preserve unrelated edits
   if Claude's OCR branch has advanced.
3. Before deployment, check for active scans/verifications and complete the
   guarded upgrade and live acceptance in [operations.md](operations.md).
   Verify the exact running image revision and runtime protections.
4. Run Check library. Inspect the scanned *How to Steal a Dragon's Sword* PDF
   from #161, a known wrong book by the same author, and affected cached OCR
   cases. Confirm ordinary good books reuse compatible proof and Activity
   links reused receipts to original verification.
5. Update this handoff with the actual deployment result and remaining work.
   Mixed audiobook folders currently have a read-only recording preview;
   applying a split remains unimplemented. The removed Automatic Mode and
   self-download workflows are outside this follow-up.

## Commands and source map

Use Python 3.12. Install development and OCR dependencies as described in
[development.md](development.md), then run:

```bash
ruff check .
PYTHONPATH=. pytest -q
bash -n scripts/acceptance/run scripts/acceptance/lib/common.sh scripts/acceptance/scenarios/*.sh
bash scripts/acceptance/run selftest
bash scripts/acceptance/run cleanup-selftest
./scripts/smoke-test.sh
sudo scripts/acceptance/run quarantine-copy-fault
```

Focused entry points: `app/verification_engine.py`, `app/identity_search.py`,
`app/provenance.py`, `app/verifier.py`, `app/ocr_probe.py`,
`app/ebook_extraction.py`, `compose.yaml` and `.github/workflows/test.yml`.
Regression coverage lives in the corresponding verification, identity,
provenance, performance, OCR and settings-persistence test modules.

Mutation acceptance uses disposable fixtures and must never target the live
library. CI success alone does not establish host-specific acceptance.
