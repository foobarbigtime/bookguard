#!/usr/bin/env bash

# shellcheck source=quarantine-unsafe.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/quarantine-unsafe.sh"

SCENARIO_PORT=${BOOKGUARD_QUARANTINE_SCHEDULING_PORT:-8807}
SCENARIO_SEED_SCRIPT=seed_quarantine_scheduling_fixture.py
SCENARIO_FAKE_SCRIPT=fake_bindery_quarantine_scheduling.py

scenario_description() {
  printf '%s\n' "Advance a second unsafe item after the first quarantine pauses; preserve unrelated media."
}

scenario_run() {
  local response status first_hash second_hash sibling_hash state remaining
  setup_disposable_quarantine
  first_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/${RELATIVE}")
  second_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Second/Second Unsafe.epub")
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")
  observe_plan
  start_bookguard automatic "quarantine_exact_media"

  bg_header "FIRST ITEM: EXACT QUARANTINE"
  response="${SCENARIO_ROOT}/first.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "first item HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "first item executed"
  [[ ! -e "${SCENARIO_ROOT}/books/${RELATIVE}" ]] || bg_die "first unsafe source remained"
  bg_assert_hash "${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub" "${first_hash}" "first quarantine"
  bg_assert_hash "${SCENARIO_ROOT}/books/Second/Second Unsafe.epub" "${second_hash}" "second item remains"
  remaining=$(docker exec "${SCENARIO_FAKE}" python -c \
    "import sqlite3; c=sqlite3.connect('/state/bindery.db'); print(c.execute('SELECT COUNT(*) FROM book_files WHERE id=9003 AND book_id=303').fetchone()[0])")
  bg_assert_eq "1" "${remaining}" "second association before its turn"

  bg_header "SECOND ITEM: OLDER PAUSED PLAN DOES NOT CLAIM THE SLOT"
  response="${SCENARIO_ROOT}/second.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "second item HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "second item executed"
  [[ ! -e "${SCENARIO_ROOT}/books/Second/Second Unsafe.epub" ]] || bg_die "second unsafe source remained"
  bg_assert_hash "${SCENARIO_ROOT}/quarantine/303/Second Unsafe.epub" "${second_hash}" "second quarantine"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" "${sibling_hash}" "unrelated media"
  assert_associations 0
  remaining=$(docker exec "${SCENARIO_FAKE}" python -c \
    "import sqlite3; c=sqlite3.connect('/state/bindery.db'); print(c.execute('SELECT COUNT(*) FROM book_files WHERE id=9003').fetchone()[0])")
  bg_assert_eq "0" "${remaining}" "second exact association detached"
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"deregisterAttempts":2' "exactly two detach requests"
  bg_assert_contains "${state}" '"deregisterSuccesses":2' "exactly two detached associations"
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"

  response="${SCENARIO_ROOT}/paused.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "completed plans HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "disabled replacement remains paused"
  bg_note "Two exact disposable quarantines completed without starvation, scans, or unrelated changes."
}
