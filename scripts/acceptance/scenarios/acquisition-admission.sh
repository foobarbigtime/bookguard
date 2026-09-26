#!/usr/bin/env bash

# Reuse the isolated writable topology and fake Bindery from publication proof.
# shellcheck source=publication-proof.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/publication-proof.sh"

SCENARIO_REACQUISITION=true
SCENARIO_SEED_SCRIPT=seed_verified_admission_fixture.py

scenario_description() {
  printf '%s\n' "Admit only exact disposable verified bytes; adopt an interrupted publication without replay."
}

setup_verified_admission() {
  setup_disposable_admission
  cat > "${SCENARIO_ROOT}/bindery-state/state.json" <<'JSON'
{"settings":{"import.mode":"external","import.drop_folder":"/data/bookguard-staging","autoGrab.enabled":"false"},"queue":[{"id":77,"bookId":101,"title":"Conflict Fixture release","protocol":"usenet","status":"importExternal"}],"partial":false,"scanAttempts":0,"scanStatus":200}
JSON
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"
}

observe_admission_plan() {
  local observed
  start_bookguard observe ""
  observed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observed}" '"planKind":"PREPARE_ACQUISITION_ADMISSION"' "exact plan"
  bg_remove_container "${SCENARIO_APP}"
}

assert_scans() {
  local expected=$1 actual
  actual=$(sed -nE 's/.*"scanAttempts"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' \
    "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_eq "${expected}" "${actual}" "Bindery scan count"
}

scenario_run() {
  local response status staged_hash
  setup_verified_admission
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")

  bg_header "EMPTY ALLOWLIST: NO ADMISSION"
  observe_admission_plan
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/empty.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "empty allowlist HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"idle"' "admission not selected"
  [[ ! -e "${SCENARIO_ROOT}/admission-books/${RELATIVE}" ]] ||
    bg_die "ebook published with empty allowlist"
  assert_scans 0
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT ALLOWLIST: ONE GUARDED PUBLICATION, NO SCAN"
  start_bookguard automatic "admit_verified_acquisition"
  response="${SCENARIO_ROOT}/admitted.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "admission HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "admission executed"
  bg_assert_contains "$(cat "${response}")" '"status":"published"' "admission journal"
  bg_assert_contains "$(cat "${response}")" '"scanRequested":false' "scan separately gated"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${RELATIVE}" "${staged_hash}" "published bytes"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staging retained"
  assert_scans 0
  response="${SCENARIO_ROOT}/replay.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "no replay HTTP"
  assert_scans 0
  bg_remove_container "${SCENARIO_APP}"

  bg_header "INTERRUPTED ADMISSION: ADOPT WITHOUT REPUBLISHING OR SCANNING"
  scenario_cleanup
  setup_verified_admission
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")
  observe_admission_plan
  start_bookguard automatic "admit_verified_acquisition"
  docker exec -i "${SCENARIO_APP}" python - \
    < "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_verified_admission_running.py"
  assert_scans 0
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard automatic "admit_verified_acquisition"
  response="${SCENARIO_ROOT}/adopted.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "interrupted outcome HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"reconciled"' "proven journal adopted"
  bg_assert_contains "$(cat "${response}")" '"reconciledAfterRestart":true' "no replay"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${RELATIVE}" "${staged_hash}" "adopted bytes"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staging retained"
  assert_scans 0
  bg_note "Disposable publication and interrupted-outcome adoption passed; Bindery scan remains separately gated."
}
