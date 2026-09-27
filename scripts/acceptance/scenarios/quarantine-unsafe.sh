#!/usr/bin/env bash

SCENARIO_APP=""
SCENARIO_FAKE=""
SCENARIO_NETWORK=""
SCENARIO_ROOT=""
SCENARIO_IMAGE=""
SCENARIO_PORT=${BOOKGUARD_QUARANTINE_UNSAFE_PORT:-8806}
AUTH_USER="bookguard"
AUTH_PASSWORD="acceptance-only"
RELATIVE="BookGuard Test/Conflict Fixture.epub"

scenario_description() {
  printf '%s\n' "Quarantine one disposable unsafe EPUB; preserve unrelated bytes and block uncertain replay."
}

scenario_requires_docker() { return 0; }
scenario_requires_acceptance_root() { return 0; }

scenario_cleanup() {
  [[ -z "${SCENARIO_APP}" ]] || bg_remove_container "${SCENARIO_APP}"
  [[ -z "${SCENARIO_FAKE}" ]] || bg_remove_container "${SCENARIO_FAKE}"
  [[ -z "${SCENARIO_NETWORK}" ]] || bg_remove_network "${SCENARIO_NETWORK}"
}

start_bookguard() {
  local mode=$1 allowlist=${2:-}
  bg_remove_container "${SCENARIO_APP}"
  docker run -d --name "${SCENARIO_APP}" --network "${SCENARIO_NETWORK}" \
    --user 99:100 --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    -p "127.0.0.1:${SCENARIO_PORT}:8788" \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/books:/books:ro" \
    -v "${SCENARIO_ROOT}/books:/action-books:rw" \
    -v "${SCENARIO_ROOT}/quarantine:/quarantine:rw" \
    -v "${SCENARIO_ROOT}/bindery-state/bindery.db:/bindery/bindery.db:ro" \
    -e CONFIG_DIR=/config \
    -e BOOKGUARD_AUTH_USERNAME="${AUTH_USER}" \
    -e BOOKGUARD_AUTH_PASSWORD="${AUTH_PASSWORD}" \
    -e BOOKGUARD_ALLOW_ACTIONS=true \
    -e BOOKGUARD_EBOOK_ACTIONS_ENABLED=true \
    -e BOOKGUARD_EBOOK_ACTION_ROOT=/action-books \
    -e BOOKGUARD_AUTOMATION_MODE="${mode}" \
    -e BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST="${allowlist}" \
    -e BINDERY_URL="http://${SCENARIO_FAKE}:8787" \
    -e BINDERY_API_KEY=acceptance \
    -e BINDERY_DB=/bindery/bindery.db \
    -e EBOOK_ROOT=/books \
    -e EBOOK_BINDERY_PREFIX=/data/media/books \
    -e QUARANTINE_ROOT=/quarantine \
    "${SCENARIO_IMAGE}" >/dev/null
  bg_wait_http "http://127.0.0.1:${SCENARIO_PORT}/health" 45 1
}

post_cycle() {
  local output=$1
  curl -sS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_AUTOMATIC_CYCLE"}' \
    -o "${output}" -w '%{http_code}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/run"
}

observe_plan() {
  local observed
  start_bookguard observe ""
  observed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observed}" '"planKind":"QUARANTINE_UNSAFE_MEDIA"' "unsafe-media plan"
  bg_remove_container "${SCENARIO_APP}"
}

setup_disposable_quarantine() {
  SCENARIO_ROOT=$(bg_reset_scenario_root "${BG_ACCEPTANCE_SCENARIO}")
  SCENARIO_APP=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" app)
  SCENARIO_FAKE=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" bindery)
  SCENARIO_NETWORK=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" net)
  bg_create_network "${SCENARIO_NETWORK}"
  SCENARIO_IMAGE=$(bg_build_image)
  mkdir -p "${SCENARIO_ROOT}/config" "${SCENARIO_ROOT}/books" \
    "${SCENARIO_ROOT}/quarantine" "${SCENARIO_ROOT}/bindery-state"
  chown -R 99:100 "${SCENARIO_ROOT}"

  docker run --rm --user 99:100 \
    -e CONFIG_DIR=/config -e EBOOK_ROOT=/books \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/books:/books:rw" \
    -v "${SCENARIO_ROOT}/bindery-state:/bindery:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/${SCENARIO_SEED_SCRIPT:-seed_unsafe_quarantine_fixture.py}:/app/seed_fixture.py:ro" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_unsafe_quarantine_fixture.py:/app/seed_unsafe_quarantine_fixture.py:ro" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_registration_conflict_fixture.py:/app/seed_registration_conflict_fixture.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_fixture.py
  printf '%s\n' '{"queue":[],"partial":false,"scanAttempts":0,"deregisterAttempts":0,"deregisterSuccesses":0}' \
    > "${SCENARIO_ROOT}/bindery-state/state.json"
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"
  docker run -d --name "${SCENARIO_FAKE}" --network "${SCENARIO_NETWORK}" \
    --user 99:100 --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/fake_bindery_registration_conflict.py:/acceptance/fake_bindery_registration_conflict.py:ro" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/fake_bindery_unsafe_quarantine.py:/acceptance/fake_bindery_unsafe_quarantine.py:ro" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/${SCENARIO_FAKE_SCRIPT:-fake_bindery_unsafe_quarantine.py}:/acceptance/fake.py:ro" \
    -e FAKE_BINDERY_STATE=/state/state.json \
    -e FAKE_BINDERY_DB=/state/bindery.db \
    -e FAKE_BINDERY_BOOK_ID=101 \
    -e FAKE_BINDERY_STORED_PATH="/data/media/books/${RELATIVE}" \
    -e PORT=8787 "${SCENARIO_IMAGE}" python /acceptance/fake.py >/dev/null
}

assert_associations() {
  local expected=$1 actual
  actual=$(docker exec "${SCENARIO_FAKE}" python -c \
    "import sqlite3; c=sqlite3.connect('/state/bindery.db'); print(c.execute('SELECT COUNT(*) FROM book_files WHERE id=9001').fetchone()[0])")
  bg_assert_eq "${expected}" "${actual}" "exact Bindery association"
  actual=$(docker exec "${SCENARIO_FAKE}" python -c \
    "import sqlite3; c=sqlite3.connect('/state/bindery.db'); print(c.execute('SELECT COUNT(*) FROM book_files WHERE id=9002 AND book_id=202').fetchone()[0])")
  bg_assert_eq "1" "${actual}" "unrelated Bindery association"
}

assert_no_other_mutations() {
  local state
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"
  bg_assert_contains "${state}" '"deregisterSuccesses":1' "one exact deregistration"
  bg_assert_contains "${state}" '"deregisterAttempts":1' "no repeated deregistration"
}

scenario_run() {
  local response status source_hash sibling_hash state old_hash verdict
  setup_disposable_quarantine
  source_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/${RELATIVE}")
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")

  bg_header "OBSERVE: DETERMINISTIC UNSAFE PLAN"
  observe_plan

  bg_header "EMPTY ALLOWLIST: NO MOVE OR DETACH"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/blocked.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "409" "${status}" "empty allowlist HTTP"
  bg_assert_contains "$(cat "${response}")" 'ACTION_NOT_ALLOWLISTED' "empty allowlist"
  bg_assert_hash "${SCENARIO_ROOT}/books/${RELATIVE}" "${source_hash}" "unsafe source"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" "${sibling_hash}" "unrelated media"
  [[ ! -e "${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub" ]] || bg_die "unexpected quarantine"
  assert_associations 1
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT ALLOWLIST: QUARANTINE AND DETACH ONE FILE"
  start_bookguard automatic "quarantine_exact_media"
  response="${SCENARIO_ROOT}/executed.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "quarantine HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "quarantine executed"
  bg_assert_contains "$(cat "${response}")" '"permanentDeletion":false' "no permanent deletion"
  bg_assert_contains "$(cat "${response}")" '"replacementRequested":false' "no replacement"
  [[ ! -e "${SCENARIO_ROOT}/books/${RELATIVE}" ]] || bg_die "unsafe source remained active"
  bg_assert_hash "${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub" "${source_hash}" "quarantined bytes"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" "${sibling_hash}" "unrelated media"
  assert_associations 0
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"deregisterSuccesses":1' "exact deregistration"
  response="${SCENARIO_ROOT}/repeat.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "repeat cycle HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "later recovery disabled"
  assert_no_other_mutations
  bg_remove_container "${SCENARIO_APP}"

  bg_header "INTERRUPTED RECEIPT: REFUSE UNCERTAIN REPLAY"
  scenario_cleanup
  setup_disposable_quarantine
  source_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/${RELATIVE}")
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")
  observe_plan
  start_bookguard automatic "quarantine_exact_media"
  docker exec -i "${SCENARIO_APP}" python - \
    < "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_unsafe_quarantine_running.py"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard automatic "quarantine_exact_media"
  response="${SCENARIO_ROOT}/interrupted.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "interrupted HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"blocked"' "uncertain work item blocked"
  bg_assert_contains "$(cat "${response}")" 'UNCERTAIN_EXTERNAL_OUTCOME' "no replay"
  bg_assert_hash "${SCENARIO_ROOT}/books/${RELATIVE}" "${source_hash}" "source after restart"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" "${sibling_hash}" "unrelated media after restart"
  [[ ! -e "${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub" ]] || bg_die "uncertain run moved source"
  assert_associations 1
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"deregisterAttempts":0' "no uncertain deregistration"

  bg_header "BINDERY OUTAGE: RESTORE THE EXACT SOURCE"
  scenario_cleanup
  setup_disposable_quarantine
  source_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/${RELATIVE}")
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")
  observe_plan
  docker exec "${SCENARIO_FAKE}" python -c \
    "import json; from pathlib import Path; p=Path('/state/state.json'); s=json.loads(p.read_text()); s['deregisterStatus']=503; p.write_text(json.dumps(s))"
  start_bookguard automatic "quarantine_exact_media"
  response="${SCENARIO_ROOT}/outage.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "Bindery outage HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"blocked"' "outage blocks work item"
  bg_assert_contains "$(cat "${response}")" 'EXECUTION_FAILED' "failed deregistration reported"
  bg_assert_hash "${SCENARIO_ROOT}/books/${RELATIVE}" "${source_hash}" "source restored after outage"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" "${sibling_hash}" "unrelated media after outage"
  [[ ! -e "${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub" ]] || bg_die "outage left a second copy"
  assert_associations 1
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"deregisterAttempts":1' "one failed deregistration"
  bg_assert_contains "${state}" '"deregisterSuccesses":0' "no deregistration succeeded"
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan on outage"

  bg_header "STALE EVIDENCE: CHANGED SOURCE BYTES BLOCK QUARANTINE"
  scenario_cleanup
  setup_disposable_quarantine
  old_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/${RELATIVE}")
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")
  observe_plan
  printf '\nchanged disposable source bytes\n' >> "${SCENARIO_ROOT}/books/${RELATIVE}"
  source_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/${RELATIVE}")
  [[ "${source_hash}" != "${old_hash}" ]] || bg_die "source did not change"
  start_bookguard automatic "quarantine_exact_media"
  verdict=$(docker exec "${SCENARIO_APP}" python -c \
    "from app.db import result_by_id; from app.verifier import verify_result; print(verify_result(result_by_id(1), force=True)['verdict'])")
  bg_assert_eq "UNSAFE_FILE" "${verdict}" "changed bytes remain deterministically unsafe"
  response="${SCENARIO_ROOT}/stale.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "stale evidence HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"blocked"' "stale plan blocked"
  bg_assert_contains "$(cat "${response}")" 'BOUNDARY_REVALIDATION_FAILED' "changed evidence refused"
  bg_assert_hash "${SCENARIO_ROOT}/books/${RELATIVE}" "${source_hash}" "changed source retained"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" "${sibling_hash}" "unrelated media after stale plan"
  [[ ! -e "${SCENARIO_ROOT}/quarantine/101/Conflict Fixture.epub" ]] || bg_die "stale plan moved source"
  assert_associations 1
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"deregisterAttempts":0' "no stale deregistration"
  bg_assert_contains "${state}" '"scanAttempts":0' "no scan on stale plan"
  bg_note "Exact quarantine, restart, outage rollback, and stale-evidence refusal passed on disposable bytes."
}
