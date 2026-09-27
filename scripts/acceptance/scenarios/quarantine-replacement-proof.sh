#!/usr/bin/env bash

# shellcheck source=quarantine-unsafe.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/quarantine-unsafe.sh"

SCENARIO_PORT=${BOOKGUARD_QUARANTINE_REPLACEMENT_PORT:-8810}

scenario_description() {
  printf '%s\n' "Prove retained disposable quarantine custody without a replacement grab."
}

scenario_run() {
  local response status plan_id preview changed sibling_hash state retained
  setup_disposable_quarantine
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")
  observe_plan
  start_bookguard automatic "quarantine_exact_media"
  response="${SCENARIO_ROOT}/quarantined.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "quarantine HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "exact quarantine"
  plan_id=$(docker exec "${SCENARIO_APP}" python -c '
from app.recovery_planner import recovery_plan_snapshot
plans = [p for p in recovery_plan_snapshot(100)["items"]
         if p["planKind"] == "QUARANTINE_UNSAFE_MEDIA"]
assert len(plans) == 1 and plans[0]["currentStep"] == 3
print(plans[0]["id"])
')

  bg_header "READ-ONLY CUSTODY PROOF"
  preview=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/plans/${plan_id}/quarantine-replacement-preview")
  bg_assert_contains "${preview}" '"safeForCandidateReview":true' "exact custody proven"
  bg_assert_contains "${preview}" '"liveGrabEnabled":false' "replacement disabled"
  retained="${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub"
  printf '\ntampered disposable quarantine\n' >> "${retained}"
  changed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/plans/${plan_id}/quarantine-replacement-preview")
  bg_assert_contains "${changed}" '"safeForCandidateReview":false' "changed bytes refused"
  bg_assert_contains "${changed}" \
    '"code":"QUARANTINE_BYTES_MATCH_RECEIPT","ok":false' "changed receipt check"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" \
    "${sibling_hash}" "unrelated media"
  assert_associations 0
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"deregisterAttempts":1' "one exact detach"
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"
  bg_note "Custody proof was read-only and refused changed bytes; no replacement was requested."
}
