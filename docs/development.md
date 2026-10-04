# Development

Use Python 3.12, matching the Docker image and CI. Run commands from the
repository root. Start with the [architecture guide](architecture.md) to find
the module responsible for the behavior you want to change.

## Install development dependencies

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt pytest httpx ruff==0.12.11
```

`httpx` is required by the application tests that use FastAPI/Starlette's
TestClient. The runtime dependencies stay in `requirements.txt`; the additional
test and lint tools match [CI](../.github/workflows/test.yml).

Install FFmpeg, including `ffprobe`, for local audio inspection. Docker provides
it through the [Dockerfile](../Dockerfile). Docker and Compose are needed for
the isolated smoke suite and container-based acceptance scenarios.

## Run tests and lint

```bash
ruff check .
PYTHONPATH=. pytest -q
```

For a focused change, run its feature tests first, then the full suite as
appropriate. Examples:

```bash
PYTHONPATH=. pytest -q tests/test_home_review.py tests/test_navigation.py
PYTHONPATH=. pytest -q tests/test_file_snapshot.py tests/test_verifier_snapshot.py
```

The suite includes synthetic files, preview/apply/Undo checks, stale evidence,
scanner failures, interrupted operations, and workflow recovery. It does not
replace host-specific deployment verification.

## Run a local UI with disposable state

Docker Compose reads `.env` for container deployment. A direct `uvicorn`
invocation does not automatically load that file. Set the application's
environment explicitly before starting it.

The following shell setup uses fresh temporary state and empty media folders.
It deliberately leaves Bindery unconfigured, so System reports that dependency
as unavailable. It is useful for UI development without a live library.

```bash
bookguard_dev_dir=$(mktemp -d /tmp/bookguard-dev.XXXXXX)
mkdir -p "$bookguard_dev_dir/config" "$bookguard_dev_dir/books" \
  "$bookguard_dev_dir/audiobooks" "$bookguard_dev_dir/quarantine" \
  "$bookguard_dev_dir/staging"

export CONFIG_DIR="$bookguard_dev_dir/config"
export BINDERY_DB="$bookguard_dev_dir/bindery-unconfigured.db"
export BINDERY_URL=http://127.0.0.1:1
export EBOOK_ROOT="$bookguard_dev_dir/books"
export AUDIOBOOK_ROOT="$bookguard_dev_dir/audiobooks"
export QUARANTINE_ROOT="$bookguard_dev_dir/quarantine"
export BOOKGUARD_STAGING_ROOT="$bookguard_dev_dir/staging"
export BOOKGUARD_AUTH_USERNAME=bookguard
export BOOKGUARD_AUTH_PASSWORD=local-development-only
export BOOKGUARD_AUTOMATION_MODE=manual
export BOOKGUARD_ALLOW_ACTIONS=false
export BOOKGUARD_EBOOK_ACTIONS_ENABLED=false
export BOOKGUARD_METADATA_REPAIR_MODE=preview
export BOOKGUARD_SCAN_ON_START=false
export BOOKGUARD_WATCH_IMPORTS=false
export BOOKGUARD_SCAN_SCHEDULE=off

uvicorn app.main:app --reload --host 127.0.0.1 --port 8788
```

Open `http://127.0.0.1:8788` with the local credentials above. This password is
only for the disposable localhost example; use your own strong password for
an actual deployment. Stop the server before removing its temporary directory.
Use a synthetic Bindery fixture to exercise populated pages; the tests contain
examples of the required tables and records.

## Smoke and acceptance checks

```bash
./scripts/smoke-test.sh
scripts/acceptance/run --list
scripts/acceptance/run selftest
scripts/acceptance/run cleanup-selftest
```

The smoke suite uses disposable data and a fake Bindery client. The
[acceptance harness](../scripts/acceptance/README.md) covers Docker peers,
restart recovery, injected failures, and idempotency. Pin the reviewed commit
with `BOOKGUARD_ACCEPTANCE_EXPECTED_SHA` when running a mutation scenario on a
host. The live ClamAV and deployed-image checks belong to the separate
[operational verification](operations.md) workflow.

CI runs lint, the Python suite, shell syntax validation, harness self-tests,
the isolated smoke suite, and the mutation scenarios listed in
[`.github/workflows/test.yml`](../.github/workflows/test.yml). Use that file as
the authoritative list rather than assuming every available scenario runs in CI.

## Keep changes understandable

- Add HTTP validation and response handling in the relevant router; put domain
  behavior in its feature module.
- Reuse current-scan, filesystem, and verification checks at mutation boundaries.
  A UI preview is not permission to reuse stale evidence when applying an action.
- Keep the serialized automation entry point in `automatic_runner.py` distinct
  from individual executors and the core execution journal.
- Cover the meaningful failure and restart cases when changing a workflow that
  has external effects. Preserve the evidence needed for reconciliation.
- Update the corresponding guide and design status when behavior changes.
- Add behavior to the existing feature instead of introducing another versioned
  entry-point wrapper such as `main_v*` or `verifier_v*`.
