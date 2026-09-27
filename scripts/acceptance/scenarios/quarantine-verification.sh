#!/usr/bin/env bash

# shellcheck source=quarantine-handoff.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/quarantine-handoff.sh"

SCENARIO_PORT=${BOOKGUARD_QUARANTINE_VERIFICATION_PORT:-8814}

scenario_description() {
  printf '%s\n' "Record verified quarantine replacement proof, with admission and final reconciliation disabled."
}

scenario_run() {
  local response status state
  setup_verified_handoff
  bg_remove_container "${SCENARIO_APP}"

  bg_header "PROOF-ONLY QUARANTINE VERIFICATION"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/proof.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "proof HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"verified"' "verified handoff"
  bg_assert_contains "$(cat "${response}")" '"externalMutationAttempted":false' "no mutation"
  bg_assert_contains "$(cat "${response}")" '"admissionAttempted":false' "no admission"
  docker exec "${SCENARIO_APP}" python -c '
from app.db import ebook_replacement_for_quarantine_plan
from app.recovery_planner import recovery_plan_by_id
plan = recovery_plan_by_id(1)
child = ebook_replacement_for_quarantine_plan(1)
assert plan and plan["currentStep"] == 5 and plan["state"] == "ready"
assert child and child["status"] == "verified" and child["admission_id"] is None
'
  response="${SCENARIO_ROOT}/final-disabled.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "later step HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "final step disabled"

  bg_assert_hash "${SCENARIO_ROOT}/staging/Conflict Fixture.epub" \
    "${SCENARIO_STAGED_HASH}" "staged bytes"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" \
    "${SCENARIO_SIBLING_HASH}" "unrelated media"
  [[ ! -e "${SCENARIO_ROOT}/books/${RELATIVE}" ]] || bg_die "Unsafe source was restored."
  assert_associations 0
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"grabAttempts":1' "one original grab"
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"
  bg_note "Exact verified replacement proof recorded; final reconciliation and admission remain disabled."
}
