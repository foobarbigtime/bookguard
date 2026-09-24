#!/usr/bin/env bash

# Reuse the isolated fake Bindery, seed and container lifecycle from the proof scenario.
# shellcheck source=publication-proof.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/publication-proof.sh"

scenario_description() {
  printf '%s\n' "Publish one verified disposable ebook after a proven no-overwrite method; never scan Bindery."
}

observe_and_prove() {
  local observed response status
  start_bookguard observe ""
  observed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observed}" '"planKind":"RECOVER_ADMISSION_PUBLICATION"' "exact plan"
  bg_remove_container "${SCENARIO_APP}"

  start_bookguard automatic "prove_supported_no_replace_method"
  response="${SCENARIO_ROOT}/proof.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "proof HTTP"
  bg_assert_contains "$(cat "${response}")" '"admissionPublished":false' "proof publishes no ebook"
  [[ ! -e "${SCENARIO_ROOT}/admission-books/${RELATIVE}" ]] || bg_die "proof published ebook"
  bg_remove_container "${SCENARIO_APP}"
}

assert_no_scan() {
  local state
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"
  [[ ! -e "${SCENARIO_ROOT}/bindery-state/scan-requested" ]] || bg_die "Bindery scan requested"
}

scenario_run() {
  local response status staged_hash record
  setup_disposable_admission
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")

  bg_header "PROVE DISPOSABLE FILESYSTEM METHOD"
  observe_and_prove

  bg_header "PROOF ALLOWLIST ALONE BLOCKS PUBLICATION"
  start_bookguard automatic "prove_supported_no_replace_method"
  response="${SCENARIO_ROOT}/blocked.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "409" "${status}" "publication not allowlisted"
  bg_assert_contains "$(cat "${response}")" 'ACTION_NOT_ALLOWLISTED' "exact allowlist required"
  [[ ! -e "${SCENARIO_ROOT}/admission-books/${RELATIVE}" ]] || bg_die "ebook published without allowlist"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT PUBLICATION ALLOWLIST: COPY, REVERIFY, PUBLISH"
  start_bookguard automatic "retry_guarded_publication"
  response="${SCENARIO_ROOT}/published.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "publication HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"published"' "durable publication"
  bg_assert_contains "$(cat "${response}")" '"binderyScanRequested":false' "no scan"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${RELATIVE}" "${staged_hash}" "published bytes"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staged bytes"
  response="${SCENARIO_ROOT}/paused.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "post-publication HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "later scan disabled"
  assert_no_scan
  bg_remove_container "${SCENARIO_APP}"

  bg_header "INTERRUPTED PUBLICATION: ADOPT VERIFIED BYTES WITHOUT REPLAY"
  scenario_cleanup
  setup_disposable_admission
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")
  observe_and_prove
  docker run --rm --user 99:100 \
    -e CONFIG_DIR=/config \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:ro" \
    -v "${SCENARIO_ROOT}/admission-books:/admission-books:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_publication_running.py:/app/seed_publication_running.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_publication_running.py
  start_bookguard automatic "retry_guarded_publication"
  response="${SCENARIO_ROOT}/reconciled.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "interrupted publication HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"reconciled"' "interrupted outcome adopted"
  bg_assert_contains "$(cat "${response}")" '"reconciledAfterRestart":true' "no replay"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${RELATIVE}" "${staged_hash}" "adopted bytes"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staging retained"
  assert_no_scan
  bg_note "Disposable publication and interrupted-outcome adoption passed without Bindery scan."
}
