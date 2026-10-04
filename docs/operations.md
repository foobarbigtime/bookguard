# Operations: updates, backups, and verification

Run host-side commands from the BookGuard Git checkout. The default Unraid
checkout is `/mnt/cache/appdata/bookguard`; persistent application state is
in a separate directory. See [installation](getting-started.md) for paths.

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
cd /mnt/cache/appdata/bookguard &&
git switch main &&
git pull --ff-only &&
bash scripts/safer-upgrade.sh upgrade --confirm DEPLOY_BOOKGUARD_UPGRADE
```

Before deployment, the helper repeats preflight, creates a transactionally
consistent BookGuard database backup with the candidate image, validates that
backup read-only, proves restore compatibility in disposable tmpfs, rechecks
the source revision, and tags the currently running image for rollback. It then
deploys `compose.yaml` plus `compose.clamav.yaml` with `--no-build`. When
`BOOKGUARD_EBOOK_ACTIONS_ENABLED=true`, it also includes `compose.actions.yaml`
to retain the explicitly enabled writable ebook alias. The upgrade topology
does not include the separate direct-admission alias.

Post-deployment acceptance fails closed unless all of these succeed:

- the running container uses the exact candidate image
- OCI version/revision/source provenance matches the checked-out source
- Docker health and the `/health` version are correct
- BookGuard remains non-root with a read-only root filesystem, bounded no-exec
  `/tmp`, dropped capabilities, no-new-privileges, read-only media/Bindery
  mounts, no writable admission alias, and a writable action alias only when
  ebook actions are enabled and it is proven to be the same host library
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
