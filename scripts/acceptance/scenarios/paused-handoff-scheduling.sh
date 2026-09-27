#!/usr/bin/env bash

# shellcheck source=quarantine-unsafe.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/quarantine-unsafe.sh"

SCENARIO_PORT=${BOOKGUARD_PAUSED_HANDOFF_PORT:-8808}

scenario_description() {
  printf '%s\n' "Let a paused E4 handoff yield to one exact disposable quarantine."
}

scenario_run() {
  local result source_hash sibling_hash state
  setup_disposable_quarantine
  source_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/${RELATIVE}")
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")
  observe_plan
  start_bookguard automatic "quarantine_exact_media"

  bg_header "PAUSED HANDOFF: NEXT READY ITEM GETS ONE LIVE SLOT"
  result=$(docker exec -i "${SCENARIO_APP}" python - \
    < "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/prove_paused_handoff_scheduling.py")
  bg_assert_contains "${result}" '"pausedHandoffs": 1' "earlier paused handoff"
  bg_assert_contains "${result}" '"planKind": "QUARANTINE_UNSAFE_MEDIA"' "later work item"
  [[ ! -e "${SCENARIO_ROOT}/books/${RELATIVE}" ]] || bg_die "unsafe source remained active"
  bg_assert_hash "${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub" \
    "${source_hash}" "exact quarantine"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" \
    "${sibling_hash}" "unrelated media"
  assert_associations 0
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"deregisterAttempts":1' "one exact detach request"
  bg_assert_contains "${state}" '"deregisterSuccesses":1' "one exact detach"
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"
  bg_note "Paused disposable handoff yielded to one exact quarantine; unrelated media stayed intact."
}
