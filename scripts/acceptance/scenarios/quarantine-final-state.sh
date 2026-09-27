#!/usr/bin/env bash

# shellcheck source=quarantine-verification.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/quarantine-verification.sh"

SCENARIO_PORT=${BOOKGUARD_QUARANTINE_FINAL_STATE_PORT:-8815}

scenario_description() {
  printf '%s\n' "Complete only the parent journal after a disposable finalized replacement proof."
}

scenario_run() {
  local response status state preview
  setup_verified_handoff
  bg_remove_container "${SCENARIO_APP}"

  bg_header "RECORD VERIFIED HANDOFF WITHOUT ADMISSION"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/verified-parent.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "verified parent HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"verified"' "verified handoff"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "SEED ALREADY FINALIZED DISPOSABLE CHILD"
  docker run --rm --user 99:100 \
    -e CONFIG_DIR=/config \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/books:/books:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_quarantine_final_state.py:/app/seed_final.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_final.py
  SCENARIO_ENABLE_ADMISSION=true

  bg_header "EMPTY ALLOWLIST: PARENT STAYS PAUSED"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/final-disabled.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "empty allowlist HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "parent paused"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT ALLOWLIST: READ-ONLY FINAL PROOF"
  start_bookguard automatic "reconcile_final_state"
  preview=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/plans/${SCENARIO_PLAN_ID}/quarantine-final-state-preview")
  bg_assert_contains "${preview}" '"safeToRecord":true' "current final-state proof"
  response="${SCENARIO_ROOT}/final-proof.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "final proof HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"completed"' "parent completed"
  bg_assert_contains "$(cat "${response}")" '"externalMutationAttempted":false' "proof only"
  docker exec "${SCENARIO_APP}" python -c '
from app.db import ebook_replacement_for_quarantine_plan
from app.recovery_planner import recovery_plan_by_id
plan = recovery_plan_by_id(1)
child = ebook_replacement_for_quarantine_plan(1)
assert plan and plan["state"] == "completed" and plan["currentStep"] == 6
assert child and child["status"] == "finalized" and child["admission_id"]
'
  bg_assert_hash "${SCENARIO_ROOT}/books/${RELATIVE}" "${SCENARIO_STAGED_HASH}" "published bytes"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" "${SCENARIO_SIBLING_HASH}" "unrelated bytes"
  [[ ! -e "${SCENARIO_ROOT}/staging/Conflict Fixture.epub" ]] || bg_die "Staged copy reappeared."
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"grabAttempts":1' "one original grab"
  bg_assert_contains "${state}" '"scanAttempts":0' "no scan during proof"
  assert_associations 0
  bg_note "Final parent proof completed locally; no Bindery or media mutation ran."
}
