#!/usr/bin/env bash

# shellcheck source=quarantine-replacement-proof.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/quarantine-replacement-proof.sh"

SCENARIO_PORT=${BOOKGUARD_QUARANTINE_SELECTION_PORT:-8811}
SCENARIO_FAKE_SCRIPT=fake_bindery_quarantine_selection.py

scenario_description() {
  printf '%s\n' "Save one disposable quarantined-book candidate; prove restart and stale-byte refusal."
}

scenario_run() {
  local response status plan_id selected preview stale state sibling_hash
  setup_disposable_quarantine
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")
  observe_plan
  start_bookguard automatic "quarantine_exact_media"
  response="${SCENARIO_ROOT}/quarantined.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "exact quarantine HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "exact quarantine"
  plan_id=$(docker exec "${SCENARIO_APP}" python -c '
from app.recovery_planner import recovery_plan_snapshot
plans = [p for p in recovery_plan_snapshot(100)["items"]
         if p["planKind"] == "QUARANTINE_UNSAFE_MEDIA"]
assert len(plans) == 1 and plans[0]["currentStep"] == 3
print(plans[0]["id"])
')

  bg_header "READ-ONLY CANDIDATE REVIEW"
  preview=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/plans/${plan_id}/quarantine-candidate-preview?candidate_guid=quarantine-alternate-guid")
  bg_assert_contains "${preview}" '"safeForReview":true' "candidate reviewed"
  bg_assert_contains "${preview}" '"liveGrabEnabled":false' "grab disabled"
  [[ "${preview}" != *disposable-release.nzb* ]] || bg_die "download URL leaked"

  bg_header "EXPLICIT CHOICE: LOCAL JOURNAL ONLY"
  selected=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"candidateGuid":"quarantine-alternate-guid","confirm":"SELECT_QUARANTINE_REPLACEMENT_CANDIDATE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/plans/${plan_id}/quarantine-candidate-selection")
  bg_assert_contains "${selected}" '"currentPlan":true' "exact plan bound"
  bg_assert_contains "${selected}" '"liveGrabEnabled":false' "no live grab"
  [[ "${selected}" != *disposable-release.nzb* ]] || bg_die "download URL persisted"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard automatic "quarantine_exact_media"
  selected=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/results/1/quarantine-candidate-selection")
  bg_assert_contains "${selected}" '"currentPlan":true' "choice survived restart"

  printf '\nchanged disposable quarantine\n' \
    >> "${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub"
  stale=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/results/1/quarantine-candidate-selection")
  bg_assert_contains "${stale}" '"currentPlan":false' "changed custody invalidates choice"
  response="${SCENARIO_ROOT}/stale-choice.json"
  status=$(curl -sS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"candidateGuid":"quarantine-alternate-guid","confirm":"SELECT_QUARANTINE_REPLACEMENT_CANDIDATE"}' \
    -o "${response}" -w '%{http_code}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/plans/${plan_id}/quarantine-candidate-selection")
  bg_assert_eq "409" "${status}" "stale bytes refuse a new choice"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" \
    "${sibling_hash}" "unrelated media"
  assert_associations 0
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"deregisterAttempts":1' "one exact detach"
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"
  bg_assert_contains "${state}" '"grabAttempts":0' "no replacement grab"
  bg_note "One local candidate choice survived restart and failed closed on changed custody."
}
