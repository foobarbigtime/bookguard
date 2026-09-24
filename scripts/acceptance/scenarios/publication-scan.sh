#!/usr/bin/env bash

# Reuse the isolated fake Bindery, fixture, and guarded publication steps.
# shellcheck source=publication-recovery.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/publication-recovery.sh"

scenario_description() {
  printf '%s\n' "Request one guarded disposable Bindery scan, then reconcile independent ownership without replay."
}

publish_disposable_fixture() {
  local response status
  observe_and_prove
  start_bookguard automatic "retry_guarded_publication"
  response="${SCENARIO_ROOT}/published.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "publication HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"published"' "durable publication"
  bg_remove_container "${SCENARIO_APP}"
  assert_no_scan
}

scan_attempts() {
  sed -nE 's/.*"scanAttempts"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' \
    "${SCENARIO_ROOT}/bindery-state/state.json"
}

scenario_run() {
  local response status staged_hash observed
  setup_disposable_admission
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")
  bg_header "GUARDED PUBLICATION WITHOUT SCAN"
  publish_disposable_fixture

  bg_header "ONE EXACT ALLOWLISTED SCAN REQUEST"
  sed -i 's/"scanAttempts":0/"scanAttempts":0,"scanStatus":200/' \
    "${SCENARIO_ROOT}/bindery-state/state.json"
  start_bookguard automatic "scan_and_reconcile_registration"
  response="${SCENARIO_ROOT}/scan.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "scan request HTTP"
  if [[ "$(cat "${response}")" != *'"status":"scan_requested"'* ]]; then
    bg_note "Unexpected scan response: $(cat "${response}")"
  fi
  bg_assert_contains "$(cat "${response}")" '"status":"scan_requested"' "durable scan request"
  bg_assert_contains "$(cat "${response}")" '"scanRequested":true' "one scan requested"
  bg_assert_eq "1" "$(scan_attempts)" "exactly one Bindery scan"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staging retained"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${RELATIVE}" "${staged_hash}" "published bytes"
  response="${SCENARIO_ROOT}/idle.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "no replay HTTP"
  bg_assert_eq "1" "$(scan_attempts)" "no duplicate scan"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "INDEPENDENT OWNER CONFIRMED BY EXISTING RECONCILER"
  docker run --rm --user 99:100 \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    "${SCENARIO_IMAGE}" python -c \
    'import sqlite3; c=sqlite3.connect("/state/bindery.db"); c.execute("INSERT INTO book_files(id,book_id,format,path) VALUES (9001,101,?,?)", ("ebook","/data/media/books/BookGuard Test/Conflict Fixture.epub")); c.commit()'
  start_bookguard observe ""
  observed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observed}" '"planKind":"RECONCILE_ADMISSION"' "separate registration plan"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard automatic "reconcile_known_admission"
  response="${SCENARIO_ROOT}/registered.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "registration HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"registered"' "registration confirmed"
  bg_assert_eq "1" "$(scan_attempts)" "no second scan during reconciliation"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "INTERRUPTED UNPROVEN SCAN: BLOCK WITHOUT RETRY"
  scenario_cleanup
  setup_disposable_admission
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")
  publish_disposable_fixture
  start_bookguard automatic "scan_and_reconcile_registration"
  docker exec -i "${SCENARIO_APP}" python - \
    < "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_publication_scan_running.py"
  response="${SCENARIO_ROOT}/uncertain.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "uncertain outcome HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"blocked"' "uncertain scan blocked"
  bg_assert_contains "$(cat "${response}")" 'UNCERTAIN_EXTERNAL_OUTCOME' "uncertain reason"
  bg_assert_eq "0" "$(scan_attempts)" "no scan retried"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staged bytes"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${RELATIVE}" "${staged_hash}" "published bytes"
  bg_note "One scan and proof-only registration passed; interrupted scan was never retried."
}
