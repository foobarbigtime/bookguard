#!/usr/bin/env bash

# shellcheck source=paused-handoff-scheduling.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/paused-handoff-scheduling.sh"

SCENARIO_PORT=${BOOKGUARD_RETRY_SCHEDULING_PORT:-8809}
SCENARIO_PROOF_SCRIPT=prove_paused_retry_scheduling.py

scenario_description() {
  printf '%s\n' "Let a completed retry with no proven handoff yield to one disposable quarantine."
}
