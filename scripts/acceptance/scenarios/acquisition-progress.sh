#!/usr/bin/env bash

SCENARIO_APP=""
SCENARIO_FAKE=""
SCENARIO_NETWORK=""
SCENARIO_ROOT=""
SCENARIO_IMAGE=""
SCENARIO_PORT=${BOOKGUARD_PROGRESS_ACCEPTANCE_PORT:-8796}
AUTH_USER="bookguard"
AUTH_PASSWORD="acceptance-only"

scenario_description() {
  printf '%s\n' "Progress a known disposable Bindery queue item to verified staging without admission."
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
  docker run -d \
    --name "${SCENARIO_APP}" --network "${SCENARIO_NETWORK}" \
    --user 99:100 --read-only --cap-drop ALL \
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
    -e BOOKGUARD_BINDERY_DROP_FOLDER=/data/bookguard-staging \
    -e BINDERY_URL="http://${SCENARIO_FAKE}:8787" \
    -e BINDERY_API_KEY=acceptance \
    -e EBOOK_ROOT=/books \
    -e EBOOK_BINDERY_PREFIX=/data/media/books \
    "${SCENARIO_IMAGE}" >/dev/null
  bg_wait_http "http://127.0.0.1:${SCENARIO_PORT}/health" 45 1
}

observe_once() {
  curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run"
}

post_cycle() {
  local output=$1
  curl -sS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"confirm":"RUN_AUTOMATIC_CYCLE"}' \
    -o "${output}" -w '%{http_code}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/run"
}

setup_progress_fixture() {
  local seed_output
  SCENARIO_ROOT=$(bg_reset_scenario_root "${BG_ACCEPTANCE_SCENARIO}")
  SCENARIO_APP=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" app)
  SCENARIO_FAKE=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" bindery)
  SCENARIO_NETWORK=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" net)
  bg_create_network "${SCENARIO_NETWORK}"
  SCENARIO_IMAGE=$(bg_build_image)

  mkdir -p "${SCENARIO_ROOT}/config" "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/books" "${SCENARIO_ROOT}/bindery-state"
  chown -R 99:100 "${SCENARIO_ROOT}/config" "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/books" "${SCENARIO_ROOT}/bindery-state"
  seed_output=$(docker run --rm --user 99:100 -e CONFIG_DIR=/config \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_acquisition_progress_fixture.py:/app/seed_progress.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_progress.py)
  bg_note "${seed_output}"
  SCENARIO_FIXTURE_HASH=$(bg_sha256 "${SCENARIO_ROOT}/staging/Progress Fixture.epub")
  cat > "${SCENARIO_ROOT}/bindery-state/state.json" <<'JSON'
{"queue":[{"id":77,"bookId":101,"title":"Progress Fixture release","protocol":"usenet","status":"importExternal"}],"partial":false,"queueStatus":200}
JSON
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"

  docker run -d --name "${SCENARIO_FAKE}" --network "${SCENARIO_NETWORK}" \
    --user 99:100 --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
    -v "${SCENARIO_ROOT}/bindery-state:/state:ro" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/fake_bindery_acquisition_progress.py:/acceptance/fake_bindery.py:ro" \
    -e FAKE_BINDERY_STATE=/state/state.json -e PORT=8787 \
    "${SCENARIO_IMAGE}" python /acceptance/fake_bindery.py >/dev/null
}

scenario_run() {
  local fixture_hash observe_body response http_status executions
  setup_progress_fixture
  fixture_hash=${SCENARIO_FIXTURE_HASH}

  bg_header "OBSERVE: PLAN ONLY"
  start_bookguard observe ""
  observe_body=$(observe_once)
  bg_assert_contains "${observe_body}" '"planKind":"RECONCILE_ACQUISITION"' "Observe plan"
  bg_assert_hash "${SCENARIO_ROOT}/staging/Progress Fixture.epub" "${fixture_hash}" "Observe bytes"
  [[ ! -e "${SCENARIO_ROOT}/books/Progress Fixture.epub" ]] ||
    bg_die "Observe unexpectedly published library bytes."
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EMPTY ALLOWLIST: NO PROGRESSION"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/empty-allowlist.json"
  http_status=$(post_cycle "${response}")
  bg_assert_eq "409" "${http_status}" "empty-allowlist HTTP"
  bg_assert_contains "$(cat "${response}")" 'ACTION_NOT_ALLOWLISTED' "allowlist refusal"
  bg_assert_hash "${SCENARIO_ROOT}/staging/Progress Fixture.epub" "${fixture_hash}" "blocked bytes"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT ALLOWLIST: OBSERVE, THEN INDEPENDENTLY VERIFY"
  start_bookguard automatic "resume_known_transition"
  response="${SCENARIO_ROOT}/first.json"
  http_status=$(post_cycle "${response}")
  bg_assert_eq "200" "${http_status}" "observation HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"staging_observed"' "first progression"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard observe ""
  observe_body=$(observe_once)
  bg_assert_contains "${observe_body}" '"planKind":"RECONCILE_ACQUISITION"' "new staged plan"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard automatic "resume_known_transition"
  response="${SCENARIO_ROOT}/second.json"
  http_status=$(post_cycle "${response}")
  bg_assert_eq "200" "${http_status}" "verification HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"verified"' "verified progression"
  bg_assert_contains "$(cat "${response}")" '"admissionAttempted":false' "no admission"
  bg_assert_hash "${SCENARIO_ROOT}/staging/Progress Fixture.epub" "${fixture_hash}" "verified bytes"
  [[ ! -e "${SCENARIO_ROOT}/books/Progress Fixture.epub" ]] ||
    bg_die "Acquisition progression unexpectedly published library bytes."

  response="${SCENARIO_ROOT}/replay.json"
  http_status=$(post_cycle "${response}")
  bg_assert_eq "200" "${http_status}" "post-verification HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"idle"' "idempotent replay"
  executions=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/executions")
  bg_assert_contains "${executions}" '"actionCode":"resume_known_transition"' "execution journal"
  bg_assert_contains "${executions}" '"state":"succeeded"' "successful journal"
  bg_note "Known-queue acquisition progress passed without Bindery mutation or admission."
}
