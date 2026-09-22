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
```

For a mutation PR, pin the exact commit that was reviewed/built:

```bash
BOOKGUARD_ACCEPTANCE_EXPECTED_SHA=<full-40-character-sha> \
  scripts/acceptance/run <scenario>
```

A scenario that needs disposable host storage defaults to `<repo>/.acceptance`. On an
Unraid host you may instead keep it outside the worktree:

```bash
BOOKGUARD_ACCEPTANCE_ROOT=/mnt/cache/appdata/bookguard-acceptance \
BOOKGUARD_ACCEPTANCE_EXPECTED_SHA=<full-sha> \
  scripts/acceptance/run <scenario>
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

CI runs shell syntax validation and the network-free `selftest` scenario. Mutation
scenarios that need Docker peers, injected crashes, or a realistic fake Bindery are still
run explicitly as acceptance evidence. As reusable fake services mature, deterministic
parts of those scenarios can be promoted into CI without removing the local acceptance
pass.

## Direction

The next scenario to build on this harness is guarded acquisition finalization/cleanup.
Once that scenario exists, the intended operator workflow is one command plus the captured
output rather than manually reconstructing fixtures and recovery state for every case.
