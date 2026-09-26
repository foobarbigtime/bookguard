#!/usr/bin/env bash

# shellcheck source=acquisition-admission.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/acquisition-admission.sh"

SCENARIO_REACQUISITION=true
SCENARIO_SEED_SCRIPT=seed_verified_admission_fixture.py

scenario_description() {
  printf '%s\n' "Retire only a proven failed local journal; separately gate new disposable publication."
}

observe_retirement() {
  local observed
  start_bookguard observe ""
  observed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observed}" '"planKind":"REVIEW_ADMISSION_PREPUBLICATION"' "failure review plan"
  bg_remove_container "${SCENARIO_APP}"
}

scenario_run() {
  local response status observed staged_hash
  setup_verified_admission
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")
  observe_admission_plan
  start_bookguard observe ""
  docker exec -i "${SCENARIO_APP}" python - \
    < "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_prepublication_failure.py"
  bg_remove_container "${SCENARIO_APP}"
  observe_retirement

  bg_header "EMPTY ALLOWLIST: FAILED JOURNAL RETAINED"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/idle.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "empty allowlist HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"idle"' "retirement gated"
  [[ ! -e "${SCENARIO_ROOT}/admission-books/${RELATIVE}" ]] || bg_die "ebook published"
  assert_scans 0
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT RETIREMENT ALLOWLIST: JOURNAL ONLY"
  start_bookguard automatic "retire_proven_prepublication_failure"
  response="${SCENARIO_ROOT}/retired.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "retirement HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "retirement executed"
  bg_assert_contains "$(cat "${response}")" '"status":"retired_before_publication"' "journal retained"
  bg_assert_contains "$(cat "${response}")" '"libraryBytesChanged":false' "no publication"
  [[ ! -e "${SCENARIO_ROOT}/admission-books/${RELATIVE}" ]] || bg_die "ebook published by retirement"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staged bytes"
  assert_scans 0
  bg_remove_container "${SCENARIO_APP}"

  bg_header "FRESH PLAN: ADMISSION REQUIRES ITS OWN ALLOWLIST"
  start_bookguard observe ""
  observed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observed}" '"planKind":"PREPARE_ACQUISITION_ADMISSION"' "fresh acquisition plan"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard automatic "retire_proven_prepublication_failure"
  response="${SCENARIO_ROOT}/no-admission.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "separate admission gate HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"idle"' "admission remains gated"
  [[ ! -e "${SCENARIO_ROOT}/admission-books/${RELATIVE}" ]] || bg_die "ebook published without admission allowlist"
  assert_scans 0
  bg_note "Disposable journal retired without publication or Bindery scan; new admission remains separately gated."
}
