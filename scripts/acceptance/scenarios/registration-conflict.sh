#!/usr/bin/env bash

SCENARIO_APP=""
SCENARIO_FAKE=""
SCENARIO_NETWORK=""
SCENARIO_ROOT=""
SCENARIO_IMAGE=""
SCENARIO_PORT=${BOOKGUARD_REGISTRATION_CONFLICT_ACCEPTANCE_PORT:-8796}
AUTH_USER="bookguard"
AUTH_PASSWORD="acceptance-only"
STORED_PATH="/data/media/books/BookGuard Test/Conflict Fixture.epub"
PUBLISHED_RELATIVE="BookGuard Test/Conflict Fixture.epub"

scenario_description() {
  printf '%s\n' "Exercise exact-path registration conflict correction, fail-closed allowlisting, and crash adoption without replay."
}

scenario_requires_docker() {
  return 0
}

scenario_requires_acceptance_root() {
  return 0
}

scenario_cleanup() {
  if [[ -n "${SCENARIO_APP}" ]]; then
    bg_remove_container "${SCENARIO_APP}"
  fi
  if [[ -n "${SCENARIO_FAKE}" ]]; then
    bg_remove_container "${SCENARIO_FAKE}"
  fi
  if [[ -n "${SCENARIO_NETWORK}" ]]; then
    bg_remove_network "${SCENARIO_NETWORK}"
  fi
}

start_fake_bindery() {
  bg_remove_container "${SCENARIO_FAKE}"
  docker run -d \
    --name "${SCENARIO_FAKE}" \
    --network "${SCENARIO_NETWORK}" \
    --user 99:100 \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/fake_bindery_registration_conflict.py:/acceptance/fake_bindery_registration_conflict.py:ro" \
    -e FAKE_BINDERY_STATE=/state/state.json \
    -e FAKE_BINDERY_DB=/state/bindery.db \
    -e FAKE_BINDERY_BOOK_ID=101 \
    -e FAKE_BINDERY_STORED_PATH="${STORED_PATH}" \
    -e PORT=8787 \
    "${SCENARIO_IMAGE}" python /acceptance/fake_bindery_registration_conflict.py >/dev/null
}

start_bookguard() {
  local mode=$1 allowlist=${2:-}
  bg_remove_container "${SCENARIO_APP}"
  docker run -d \
    --name "${SCENARIO_APP}" \
    --network "${SCENARIO_NETWORK}" \
    --user 99:100 \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    -p "127.0.0.1:${SCENARIO_PORT}:8788" \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:ro" \
    -v "${SCENARIO_ROOT}/admission-books:/admission-books:ro" \
    -v "${SCENARIO_ROOT}/bindery-state/bindery.db:/bindery/bindery.db:ro" \
    -e CONFIG_DIR=/config \
    -e BOOKGUARD_AUTH_USERNAME="${AUTH_USER}" \
    -e BOOKGUARD_AUTH_PASSWORD="${AUTH_PASSWORD}" \
    -e BOOKGUARD_ALLOW_ACTIONS=true \
    -e BOOKGUARD_AUTOMATION_MODE="${mode}" \
    -e BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST="${allowlist}" \
    -e BOOKGUARD_AUTOMATIC_REACQUISITION=true \
    -e BOOKGUARD_ADMISSION_ENABLED=true \
    -e BOOKGUARD_STAGING_ROOT=/staging \
    -e BOOKGUARD_ADMISSION_ROOT=/admission-books \
    -e BOOKGUARD_ADMISSION_BINDERY_ROOT=/data/media/books \
    -e BINDERY_URL="http://${SCENARIO_FAKE}:8787" \
    -e BINDERY_API_KEY=acceptance \
    -e BINDERY_DB=/bindery/bindery.db \
    -e EBOOK_ROOT=/admission-books \
    -e EBOOK_BINDERY_PREFIX=/data/media/books \
    "${SCENARIO_IMAGE}" >/dev/null
  bg_wait_http "http://127.0.0.1:${SCENARIO_PORT}/health" 45 1
}

post_cycle() {
  local output=$1
  curl -sS \
    -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"confirm":"RUN_AUTOMATIC_CYCLE"}' \
    -o "${output}" \
    -w '%{http_code}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/run"
}

seed_fixture() {
  bg_remove_container "${SCENARIO_APP}"
  bg_remove_container "${SCENARIO_FAKE}"
  rm -rf \
    "${SCENARIO_ROOT}/config" \
    "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/admission-books" \
    "${SCENARIO_ROOT}/bindery-state"
  mkdir -p \
    "${SCENARIO_ROOT}/config" \
    "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/admission-books" \
    "${SCENARIO_ROOT}/bindery-state"
  chown -R 99:100 \
    "${SCENARIO_ROOT}/config" \
    "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/admission-books" \
    "${SCENARIO_ROOT}/bindery-state"

  docker run --rm \
    --user 99:100 \
    -e CONFIG_DIR=/config \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${SCENARIO_ROOT}/admission-books:/admission-books:rw" \
    -v "${SCENARIO_ROOT}/bindery-state:/bindery:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_registration_conflict_fixture.py:/app/seed_registration_conflict_fixture.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_registration_conflict_fixture.py

  cat > "${SCENARIO_ROOT}/bindery-state/state.json" <<'JSON'
{"settings":{"import.mode":"external","autoGrab.enabled":"false"},"queue":[{"id":77,"bookId":101,"title":"Conflict Fixture release","protocol":"usenet","status":"importexternal"}],"partial":false,"previewAttempts":0,"deleteAttempts":0,"deleteSuccesses":0,"reassignAttempts":0,"reassignSuccesses":0,"settingWrites":0,"reassignStatus":200}
JSON
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"
  start_fake_bindery
}

observe_once() {
  local output
  start_bookguard observe ""
  output=$(curl -fsS \
    -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${output}" '"planKind":"CORRECT_REGISTRATION_CONFLICT"' "observe correction plan"
  bg_assert_contains "${output}" '"decision":"would_correct_registration_conflict"' "observe correction decision"
  bg_remove_container "${SCENARIO_APP}"
}

db_owner() {
  docker run --rm \
    --user 99:100 \
    -v "${SCENARIO_ROOT}/bindery-state:/state:ro" \
    "${SCENARIO_IMAGE}" \
    python -c 'import sqlite3; c=sqlite3.connect("/state/bindery.db"); print(c.execute("SELECT book_id FROM book_files WHERE path=? AND format=\"ebook\"", ("/data/media/books/BookGuard Test/Conflict Fixture.epub",)).fetchone()[0])'
}

seed_running_journal() {
  docker run --rm \
    --user 99:100 \
    -e CONFIG_DIR=/config \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_registration_conflict_running.py:/app/seed_registration_conflict_running.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_registration_conflict_running.py
}

force_correct_owner() {
  docker run --rm \
    --user 99:100 \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    "${SCENARIO_IMAGE}" \
    python -c 'import sqlite3; c=sqlite3.connect("/state/bindery.db"); c.execute("UPDATE book_files SET book_id=101 WHERE path=? AND format=\"ebook\"", ("/data/media/books/BookGuard Test/Conflict Fixture.epub",)); c.commit()'
}

scenario_run() {
  local fixture_hash state_json blocked_body blocked_status execute_body execute_status
  local replay_body replay_status crash_body crash_status adopt_body adopt_status executions

  SCENARIO_ROOT=$(bg_reset_scenario_root "${BG_ACCEPTANCE_SCENARIO}")
  SCENARIO_APP=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" app)
  SCENARIO_FAKE=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" bindery)
  SCENARIO_NETWORK=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" net)

  bg_create_network "${SCENARIO_NETWORK}"
  SCENARIO_IMAGE=$(bg_build_image)

  bg_header "SEED DISPOSABLE REGISTRATION CONFLICT FIXTURE"
  seed_fixture
  fixture_hash=$(bg_sha256 "${SCENARIO_ROOT}/admission-books/${PUBLISHED_RELATIVE}")
  bg_assert_hash "${SCENARIO_ROOT}/staging/${PUBLISHED_RELATIVE}" "${fixture_hash}" "staged fixture"
  bg_assert_eq "202" "$(db_owner)" "initial foreign Bindery owner"

  bg_header "OBSERVE: PLAN EXACT CORRECTION WITHOUT MUTATION"
  observe_once
  state_json=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"reassignAttempts":0' "observe reassign count"
  bg_assert_contains "${state_json}" '"deleteAttempts":0' "observe queue-delete count"
  bg_assert_eq "202" "$(db_owner)" "owner after observe"

  bg_header "AUTOMATIC: EMPTY ALLOWLIST FAILS CLOSED"
  start_bookguard automatic ""
  blocked_body="${SCENARIO_ROOT}/blocked.json"
  blocked_status=$(post_cycle "${blocked_body}")
  bg_assert_eq "409" "${blocked_status}" "empty-allowlist HTTP status"
  bg_assert_contains "$(cat "${blocked_body}")" 'ACTION_NOT_ALLOWLISTED' "empty-allowlist response"
  state_json=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"reassignAttempts":0' "blocked reassign count"
  bg_assert_contains "${state_json}" '"deleteAttempts":0' "blocked queue-delete count"
  bg_assert_contains "${state_json}" '"settingWrites":0' "blocked setting-write count"
  bg_assert_eq "202" "$(db_owner)" "owner after blocked cycle"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${PUBLISHED_RELATIVE}" "${fixture_hash}" "staging after blocked cycle"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${PUBLISHED_RELATIVE}" "${fixture_hash}" "published bytes after blocked cycle"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "AUTOMATIC: EXACT ALLOWLIST CORRECTS OWNER WITHOUT MOVING BYTES"
  start_bookguard automatic "correct_exact_registration_owner"
  execute_body="${SCENARIO_ROOT}/execute.json"
  execute_status=$(post_cycle "${execute_body}")
  bg_assert_eq "200" "${execute_status}" "allowlisted correction HTTP status"
  bg_assert_contains "$(cat "${execute_body}")" '"state":"executed"' "allowlisted correction response"
  bg_assert_contains "$(cat "${execute_body}")" '"queueRecordRemoved":true' "queue correction result"
  bg_assert_contains "$(cat "${execute_body}")" '"libraryBytesChanged":false' "library-byte invariant"
  bg_assert_eq "101" "$(db_owner)" "corrected Bindery owner"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${PUBLISHED_RELATIVE}" "${fixture_hash}" "staging after correction"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${PUBLISHED_RELATIVE}" "${fixture_hash}" "published bytes after correction"
  state_json=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"reassignAttempts":1' "reassignment attempts"
  bg_assert_contains "${state_json}" '"reassignSuccesses":1' "reassignment successes"
  bg_assert_contains "${state_json}" '"deleteAttempts":1' "queue-delete attempts"
  bg_assert_contains "${state_json}" '"deleteSuccesses":1' "queue-delete successes"
  bg_assert_contains "${state_json}" '"lastRemoveFromClient":"false"' "download-client preservation"
  bg_assert_contains "${state_json}" '"lastDeleteFiles":"false"' "downloaded-data preservation"
  bg_assert_contains "${state_json}" '"import.mode":"external"' "restored external import mode"
  bg_assert_contains "${state_json}" '"autoGrab.enabled":"false"' "auto-grab remains disabled"

  bg_header "REPLAY: COMPLETED CORRECTION IS IDEMPOTENT"
  replay_body="${SCENARIO_ROOT}/replay.json"
  replay_status=$(post_cycle "${replay_body}")
  bg_assert_eq "200" "${replay_status}" "post-correction cycle HTTP status"
  bg_assert_contains "$(cat "${replay_body}")" '"state":"idle"' "post-correction idle response"
  state_json=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"reassignAttempts":1' "no duplicate reassignment"
  bg_assert_contains "${state_json}" '"deleteAttempts":1' "no duplicate queue deletion"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "CRASH: UNPROVEN RUNNING CORRECTION NEVER REPLAYS"
  seed_fixture
  fixture_hash=$(bg_sha256 "${SCENARIO_ROOT}/admission-books/${PUBLISHED_RELATIVE}")
  observe_once
  seed_running_journal
  start_bookguard automatic "correct_exact_registration_owner"
  crash_body="${SCENARIO_ROOT}/crash-unproven.json"
  crash_status=$(post_cycle "${crash_body}")
  bg_assert_eq "200" "${crash_status}" "unproven crash HTTP status"
  bg_assert_contains "$(cat "${crash_body}")" '"state":"blocked"' "unproven crash state"
  bg_assert_contains "$(cat "${crash_body}")" 'UNCERTAIN_EXTERNAL_OUTCOME' "unproven crash reason"
  state_json=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"reassignAttempts":0' "unproven crash no replay"
  bg_assert_contains "${state_json}" '"deleteAttempts":0' "unproven crash no queue replay"
  bg_assert_eq "202" "$(db_owner)" "unproven crash foreign owner retained"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${PUBLISHED_RELATIVE}" "${fixture_hash}" "staging after unproven crash"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${PUBLISHED_RELATIVE}" "${fixture_hash}" "published bytes after unproven crash"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "CRASH: PROVEN TARGET OWNER IS ADOPTED WITHOUT REPLAY"
  seed_fixture
  fixture_hash=$(bg_sha256 "${SCENARIO_ROOT}/admission-books/${PUBLISHED_RELATIVE}")
  observe_once
  seed_running_journal
  force_correct_owner
  bg_assert_eq "101" "$(db_owner)" "synthetically proven corrected owner"
  start_bookguard automatic "correct_exact_registration_owner"
  adopt_body="${SCENARIO_ROOT}/crash-proven.json"
  adopt_status=$(post_cycle "${adopt_body}")
  bg_assert_eq "200" "${adopt_status}" "proven crash HTTP status"
  bg_assert_contains "$(cat "${adopt_body}")" '"state":"reconciled"' "proven crash adoption state"
  bg_assert_contains "$(cat "${adopt_body}")" '"externalMutationPerformed":false' "proven crash no mutation"
  state_json=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"reassignAttempts":0' "proven crash no reassignment replay"
  bg_assert_contains "${state_json}" '"deleteAttempts":0' "proven crash no queue replay"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${PUBLISHED_RELATIVE}" "${fixture_hash}" "staging after proven crash"
  bg_assert_hash "${SCENARIO_ROOT}/admission-books/${PUBLISHED_RELATIVE}" "${fixture_hash}" "published bytes after proven crash"

  executions=$(curl -fsS \
    -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/executions")
  bg_assert_contains "${executions}" '"actionCode":"correct_exact_registration_owner"' "execution journal"
  bg_assert_contains "${executions}" '"state":"succeeded"' "proven crash journal state"

  bg_note "Registration-conflict acceptance matrix passed: Observe-only planning, empty allowlist, exact no-move correction, byte preservation, idempotency, uncertain-crash block, and proof-based crash adoption."
}
