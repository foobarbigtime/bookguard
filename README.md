# BookGuard

BookGuard checks whether the files in your [Bindery](https://github.com/vavallee/bindery)
library contain the books they are assigned to. It helps you review damaged,
misfiled, duplicate, or incorrectly tagged ebooks and audiobooks, and provides
guarded actions to resolve supported problems.

Start by scanning and reviewing. Bindery actions are off by default, metadata
repair starts in preview mode, and automation starts in manual mode with an
empty action allowlist. Scans and verification do not alter library media or
Bindery registrations; they record evidence in BookGuard's own database.

## Everyday workflow

1. Open **Home** to see health, what needs attention, and recent activity.
2. Run **Scan library** for existing books, then open **Review**.
3. Verify undecided files and inspect the evidence before choosing an action.
4. Use **Activity** to check the outcome, view records, undo metadata repairs,
   or put back an eligible quarantined file.
5. Use **System** for health problems and scheduled tasks, and **Settings** for
   supported configuration changes.

Review displays 100 books per page, with filters and totals covering the full
open review list. BookGuard may leave a case undecided when the available
information does not prove its identity.

See the [user guide](docs/user-guide.md) for the pages and available actions.

## Unraid quick start

Prerequisites: Git, Docker with Compose, a Bindery installation, and readable
library folders. Follow [Getting started](docs/getting-started.md) to configure
the password, network binding, mount paths, and writable-directory permissions.

```bash
cd /mnt/cache/appdata
git clone https://github.com/foobarbigtime/bookguard.git
cd bookguard
cp .env.example .env
# Edit .env and check compose.yaml before building.
bash scripts/build-with-provenance.sh
docker compose up -d --no-build
```

The base deployment does not enable ClamAV. The [configuration guide](docs/configuration.md)
explains the optional private scanner overlay. The supplied network binding is
localhost; configure the Unraid LAN address before opening BookGuard from
another device at `http://<unraid-ip>:8788`.

## Updating

For an existing installation on `main`:

```bash
cd /mnt/cache/appdata/bookguard &&
git switch main &&
git pull --ff-only &&
bash scripts/safer-upgrade.sh upgrade --confirm DEPLOY_BOOKGUARD_UPGRADE
```

The helper checks the source, builds with its exact Git revision, validates a
database backup, preserves the running image for rollback, deploys, and verifies
health, runtime hardening, and ClamAV. It preserves the ebook action alias when
that feature is enabled. See [operations](docs/operations.md#updating) for the
required topology and explicit recovery behavior.

## Safety model

BookGuard requires authentication, checks request origins, and uses separate
permissions for Bindery actions, metadata repair, ebook actions, and admission.
A preview or cached verdict does not authorize a later write: guarded actions
recheck the current evidence and their own preconditions.

Quarantine removes a file from the library into quarantine; Detach removes its
Bindery association while leaving the file in place. Eligible quarantines have
**Put back** in Activity. Publication refuses to overwrite an existing file,
and uncertain interrupted operations remain visible for review. See
[safety and permissions](docs/safety.md) and
[advanced workflows](docs/advanced-workflows.md) for the exact limits.

## Documentation

| Guide | Use it for |
|---|---|
| [Getting started](docs/getting-started.md) | Unraid installation, authentication, paths, and first scan |
| [User guide](docs/user-guide.md) | Home, Review, Activity, System, and everyday actions |
| [Configuration](docs/configuration.md) | Persisted settings, deployment settings, and optional ClamAV |
| [Operations](docs/operations.md) | Upgrades, database backups, smoke tests, and deployment verification |
| [Safety and permissions](docs/safety.md) | Action gates, writable aliases, identity rules, and repair limits |
| [Advanced workflows](docs/advanced-workflows.md) | Import checks, scheduling, conflicts, staging, and supervised recovery |
| [Architecture](docs/architecture.md) | Data flow, module responsibilities, journals, and change entry points |
| [Development](docs/development.md) | Local setup, tests, lint, and contribution checks |
| [Acceptance harness](scripts/acceptance/README.md) | Disposable Docker, restart, and failure scenarios |
| [Release history](docs/release-history.md) | v0.5 and v0.6 foundations |

Design proposals live in [`docs/design/`](docs/design/). Their status identifies
what has shipped and what remains proposed; they are not setup instructions.

## Development

Use Python 3.12 and the [development guide](docs/development.md). It includes
`pytest`, `httpx`, and the CI lint version, plus an isolated local server setup.

### Project structure

- [`app/main.py`](app/main.py): application startup and composition.
- [`app/routes/`](app/routes/): page and API endpoints.
- [`app/`](app/): scanning, evidence, verification, guarded workflows, and persistence.
- [`templates/`](templates/) and [`static/`](static/): server-rendered pages and browser controllers.
- [`tests/`](tests/): feature and regression tests.
- [`tools/`](tools/) and [`scripts/`](scripts/): operational tools and acceptance checks.

The [architecture guide](docs/architecture.md) maps each workflow to its modules.
