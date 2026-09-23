#!/usr/bin/env bash

SCENARIO_APP=""
SCENARIO_FAKE=""
SCENARIO_NETWORK=""
SCENARIO_ROOT=""
SCENARIO_IMAGE=""
SCENARIO_PORT=${BOOKGUARD_FINALIZATION_ACCEPTANCE_PORT:-8795}
AUTH_USER="bookguard"
AUTH_PASSWORD="acceptance-only"

scenario_description() {
  printf '%s\n' "Exercise guarded acquisition finalization against disposable BookGuard and fake Bindery state."
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
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${SCENARIO_ROOT}/books:/books:ro" \
    -e CONFIG_DIR=/config \
    -e BOOKGUARD_AUTH_USERNAME="${AUTH_USER}" \
    -e BOOKGUARD_AUTH_PASSWORD="${AUTH_PASSWORD}" \
    -e BOOKGUARD_ALLOW_ACTIONS=true \
    -e BOOKGUARD_AUTOMATION_MODE="${mode}" \
    -e BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST="${allowlist}" \
    -e BOOKGUARD_AUTOMATIC_REACQUISITION=true \
    -e BOOKGUARD_STAGING_ROOT=/staging \
    -e BINDERY_URL="http://${SCENARIO_FAKE}:8787" \
    -e BINDERY_API_KEY=acceptance \
    -e EBOOK_ROOT=/books \
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

scenario_run() {
  local seed_output fixture_hash observe_body blocked_body blocked_status
  local execute_body execute_status replay_body replay_status state_json executions

  SCENARIO_ROOT=$(bg_reset_scenario_root "${BG_ACCEPTANCE_SCENARIO}")
  SCENARIO_APP=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" app)
  SCENARIO_FAKE=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" bindery)
  SCENARIO_NETWORK=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" net)

  bg_create_network "${SCENARIO_NETWORK}"
  SCENARIO_IMAGE=$(bg_build_image)

  mkdir -p \
    "${SCENARIO_ROOT}/config" \
    "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/books" \
    "${SCENARIO_ROOT}/bindery-state"
  chown -R 99:100 \
    "${SCENARIO_ROOT}/config" \
    "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/books" \
    "${SCENARIO_ROOT}/bindery-state"

  bg_header "SEED DISPOSABLE FINALIZATION FIXTURE"
  seed_output=$(docker run --rm \
    --user 99:100 \
    -e CONFIG_DIR=/config \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${SCENARIO_ROOT}/books:/books:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_finalization_fixture.py:/seed.py:ro" \
    "${SCENARIO_IMAGE}" python /seed.py)
  bg_note "${seed_output}"

  fixture_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/BookGuard Test/Finalization Fixture.epub")
  bg_assert_hash \
    "${SCENARIO_ROOT}/staging/BookGuard Test/Finalization Fixture.epub" \
    "${fixture_hash}" \
    "staged fixture before cleanup"

  cat > "${SCENARIO_ROOT}/bindery-state/state.json" <<'JSON'
{"storedPath":"/data/media/books/BookGuard Test/Finalization Fixture.epub","registered":true,"queue":[{"id":77,"bookId":101,"title":"Finalization Fixture release","protocol":"usenet","status":"imported"}],"partial":false,"deleteAttempts":0,"deleteSuccesses":0,"deleteStatus":204,"getBookStatus":200,"queueStatus":200}
JSON
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"

  docker run -d \
    --name "${SCENARIO_FAKE}" \
    --network "${SCENARIO_NETWORK}" \
    --user 99:100 \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/fake_bindery_finalization.py:/acceptance/fake_bindery_finalization.py:ro" \
    -e FAKE_BINDERY_STATE=/state/state.json \
    -e FAKE_BINDERY_BOOK_ID=101 \
    -e PORT=8787 \
    "${SCENARIO_IMAGE}" python /acceptance/fake_bindery_finalization.py >/dev/null

  bg_header "OBSERVE: PROVE FINALIZATION PLAN WITHOUT MUTATION"
  start_bookguard observe ""
  observe_body=$(curl -fsS \
    -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observe_body}" '"planKind":"FINALIZE_ACQUISITION"' "observe plan"
  bg_assert_contains "${observe_body}" '"decision":"would_finalize_acquisition"' "observe decision"
  bg_assert_hash \
    "${SCENARIO_ROOT}/staging/BookGuard Test/Finalization Fixture.epub" \
    "${fixture_hash}" \
    "staging after observe"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "AUTOMATIC: EMPTY ALLOWLIST FAILS CLOSED"
  start_bookguard automatic ""
  blocked_body="${SCENARIO_ROOT}/blocked.json"
  blocked_status=$(post_cycle "${blocked_body}")
  bg_assert_eq "409" "${blocked_status}" "empty-allowlist HTTP status"
  bg_assert_contains "$(cat "${blocked_body}")" 'ACTION_NOT_ALLOWLISTED' "empty-allowlist response"
  bg_assert_hash \
    "${SCENARIO_ROOT}/staging/BookGuard Test/Finalization Fixture.epub" \
    "${fixture_hash}" \
    "staging after blocked cycle"
  state_json=$(cat "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"deleteAttempts": 0' "fake Bindery mutation count"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "AUTOMATIC: EXACT ALLOWLIST PERFORMS GUARDED CLEANUP"
  start_bookguard automatic "finish_guarded_cleanup"
  execute_body="${SCENARIO_ROOT}/execute.json"
  execute_status=$(post_cycle "${execute_body}")
  bg_assert_eq "200" "${execute_status}" "allowlisted finalization HTTP status"
  bg_assert_contains "$(cat "${execute_body}")" '"state":"executed"' "allowlisted finalization response"
  bg_assert_contains "$(cat "${execute_body}")" '"queueMutationPerformed":true' "queue cleanup result"
  bg_assert_contains "$(cat "${execute_body}")" '"stagedMutationPerformed":true' "staging cleanup result"

  if [[ -e "${SCENARIO_ROOT}/staging/BookGuard Test/Finalization Fixture.epub" ]]; then
    bg_die "Verified staged fixture still exists after successful finalization."
    return 1
  fi
  bg_assert_hash \
    "${SCENARIO_ROOT}/books/BookGuard Test/Finalization Fixture.epub" \
    "${fixture_hash}" \
    "library bytes after finalization"

  state_json=$(cat "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"deleteAttempts": 1' "fake Bindery delete attempts"
  bg_assert_contains "${state_json}" '"deleteSuccesses": 1' "fake Bindery successful deletes"
  bg_assert_contains "${state_json}" '"lastRemoveFromClient": "false"' "download-client preservation flag"
  bg_assert_contains "${state_json}" '"lastDeleteFiles": "false"' "downloaded-data preservation flag"
  bg_assert_contains "${state_json}" '"queue": []' "terminal queue removal"

  bg_header "REPLAY: COMPLETED PLAN IS IDEMPOTENT"
  replay_body="${SCENARIO_ROOT}/replay.json"
  replay_status=$(post_cycle "${replay_body}")
  bg_assert_eq "200" "${replay_status}" "post-finalization cycle HTTP status"
  bg_assert_contains "$(cat "${replay_body}")" '"state":"idle"' "post-finalization idle response"
  state_json=$(cat "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state_json}" '"deleteAttempts": 1' "no duplicate queue cleanup"
  bg_assert_hash \
    "${SCENARIO_ROOT}/books/BookGuard Test/Finalization Fixture.epub" \
    "${fixture_hash}" \
    "library bytes after replay"

  executions=$(curl -fsS \
    -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/executions")
  bg_assert_contains "${executions}" '"actionCode":"finish_guarded_cleanup"' "execution journal"
  bg_assert_contains "${executions}" '"state":"succeeded"' "execution journal state"

  bg_note "Finalization acceptance matrix passed: observe-only planning, empty allowlist, exact cleanup, byte preservation, and idempotency."
}
