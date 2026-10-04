# BookGuard acceptance harness

This directory is the reusable local acceptance layer for E4 and later mutation work.
It is intentionally separate from unit tests: the harness is for disposable Docker,
filesystem, restart, dependency-failure, and idempotency scenarios that are most useful
when exercised on the same host/runtime shape used for BookGuard.

The goal is **less manual setup, not less testing**.

## Quick start

```bash
scripts/acceptance/run --list
scripts/acceptance/run selftest
BOOKGUARD_ACCEPTANCE_EXPECTED_SHA="replace-with-reviewed-40-character-sha" \
  scripts/acceptance/run quarantine-copy-fault
```

For a mutation PR, replace the quoted placeholder with the exact commit that
was reviewed/built. Replace `quarantine-copy-fault` with the scenario you need:

```bash
BOOKGUARD_ACCEPTANCE_EXPECTED_SHA="replace-with-reviewed-40-character-sha" \
  scripts/acceptance/run quarantine-copy-fault
```

A scenario that needs disposable host storage defaults to `<repo>/.acceptance`. On an
Unraid host you may instead keep it outside the worktree:

```bash
BOOKGUARD_ACCEPTANCE_ROOT=/mnt/cache/appdata/bookguard-acceptance \
BOOKGUARD_ACCEPTANCE_EXPECTED_SHA="replace-with-reviewed-40-character-sha" \
  scripts/acceptance/run quarantine-copy-fault
```

## Safety rules

The harness is designed to fail closed around local data:

- the acceptance root must be an absolute path whose basename contains `acceptance`;
- `/` is always refused;
- the repository root itself is refused;
- `/mnt/cache/appdata/bookguard` is treated as the protected production root by default;
- the protected production root and anything below it are refused;
- disposable reset operations require a harness marker file;
- Docker resources use the `bookguard-acceptance-` namespace;
- mutation scenarios can require an exact Git commit through
  `BOOKGUARD_ACCEPTANCE_EXPECTED_SHA`;
- scenarios are responsible for using only disposable Bindery/BookGuard fixtures.

If production is stored somewhere else, set `BOOKGUARD_PRODUCTION_ROOT` before running
mutation scenarios.

## What belongs here

A live mutation scenario should normally exercise more than the happy path. Depending
on the mutation class, the matrix should include:

- normal success;
- empty allowlist / disabled mutation;
- stale identity, path, evidence, or byte checks;
- conflicting ownership or registration state;
- dependency/API outage;
- failure immediately before and after the external mutation;
- hard restart with a `running` execution journal;
- proof-based restart adoption when completion can be independently established;
- refusal to replay when completion cannot be proven;
- repeated-cycle idempotency;
- staged/published hash invariants;
- unrelated fixture state unchanged.

The exact matrix belongs in the scenario and its PR acceptance plan.

## Scenario contract

Each `scripts/acceptance/scenarios/<name>.sh` file defines:

```bash
scenario_description() { ...; }
scenario_run() { ...; }
```

Optional hooks are:

```bash
scenario_requires_docker() { return 0; }
scenario_requires_acceptance_root() { return 0; }
scenario_cleanup() { ...; }
```

Shared helpers live in `lib/common.sh`. A starter file is provided as
`scenarios/_template.sh.example`.

`scenario_cleanup` runs on both success and failure. Scenario cleanup should remove only
resources created by that scenario; it must never touch production containers, databases,
or media.

## CI

CI runs lint and Python tests, shell syntax validation, `selftest`,
`cleanup-selftest`, the isolated smoke suite, and a set of disposable mutation
scenarios. The exact current list is in
[`.github/workflows/test.yml`](../../.github/workflows/test.yml); use it as the
source of truth. `scripts/acceptance/run --list` lists all available scenarios,
including ones that are not part of CI.

CI fixtures do not mount the production library. Host-specific acceptance still
matters for a mutation release: pin the reviewed commit, use a marked disposable
acceptance root, and run the relevant failure/restart scenarios on the target
runtime. Live deployment provenance, hardening, and ClamAV/EICAR checks remain
separate in [the upgrade and verification workflow](../../docs/operations.md).
