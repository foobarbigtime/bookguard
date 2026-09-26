#!/usr/bin/env bash

# shellcheck source=acquisition-admission.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/acquisition-admission.sh"

scenario_description() {
  printf '%s\n' "Request one separately allowlisted scan for disposable published acquisition bytes."
}

observe_scan_plan() {
  local observed
  start_bookguard observe ""
  observed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observed}" '"planKind":"REQUEST_PUBLISHED_ACQUISITION_SCAN"' "scan plan"
  bg_remove_container "${SCENARIO_APP}"
}

scenario_run() {
  local response status staged_hash
  setup_verified_admission
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")
  observe_admission_plan
  start_bookguard automatic "admit_verified_acquisition"
  response="${SCENARIO_ROOT}/published.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "publication HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"published"' "published journal"
  assert_scans 0
  bg_remove_container "${SCENARIO_APP}"
  observe_scan_plan

  bg_header "EMPTY ALLOWLIST: NO SCAN"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/empty-scan.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "empty scan allowlist HTTP"
  assert_scans 0
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT ALLOWLIST: ONE SCAN, NO BYTE CHANGE"
  start_bookguard automatic "request_published_acquisition_scan"
  response="${SCENARIO_ROOT}/scan.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "scan HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"scan_requested"' "scan journal"
  assert_scans 1
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${RELATIVE}" "${staged_hash}" "published bytes"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staging retained"
  response="${SCENARIO_ROOT}/scan-replay.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "post-scan cycle HTTP"
  assert_scans 1
  bg_note "Disposable published acquisition scan passed with one request and unchanged bytes."
}
