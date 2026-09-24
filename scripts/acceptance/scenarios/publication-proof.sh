#!/usr/bin/env bash

SCENARIO_APP=""
SCENARIO_FAKE=""
SCENARIO_NETWORK=""
SCENARIO_ROOT=""
SCENARIO_IMAGE=""
SCENARIO_PORT=${BOOKGUARD_PUBLICATION_PROOF_PORT:-8796}
AUTH_USER="bookguard"
AUTH_PASSWORD="acceptance-only"
RELATIVE="BookGuard Test/Conflict Fixture.epub"

scenario_description() {
  printf '%s\n' "Prove no-replace on disposable filesystem bytes; never publish an ebook or scan Bindery."
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
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${SCENARIO_ROOT}/admission-books:/admission-books:rw" \
    -v "${SCENARIO_ROOT}/bindery-state/bindery.db:/bindery/bindery.db:ro" \
    -e CONFIG_DIR=/config \
    -e BOOKGUARD_AUTH_USERNAME="${AUTH_USER}" \
    -e BOOKGUARD_AUTH_PASSWORD="${AUTH_PASSWORD}" \
    -e BOOKGUARD_ALLOW_ACTIONS=true \
    -e BOOKGUARD_AUTOMATION_MODE="${mode}" \
    -e BOOKGUARD_AUTOMATIC_ACTION_ALLOWLIST="${allowlist}" \
    -e BOOKGUARD_ADMISSION_ENABLED=true \
    -e BOOKGUARD_STAGING_ROOT=/staging \
    -e BOOKGUARD_ADMISSION_ROOT=/admission-books \
    -e BOOKGUARD_ADMISSION_BINDERY_ROOT=/data/media/books \
    -e BOOKGUARD_BINDERY_DROP_FOLDER=/data/bookguard-staging \
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
  curl -sS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"confirm":"RUN_AUTOMATIC_CYCLE"}' \
    -o "${output}" -w '%{http_code}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/run"
}

scenario_run() {
  local response status staged_hash observed state
  SCENARIO_ROOT=$(bg_reset_scenario_root "${BG_ACCEPTANCE_SCENARIO}")
  SCENARIO_APP=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" app)
  SCENARIO_FAKE=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" bindery)
  SCENARIO_NETWORK=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" net)
  bg_create_network "${SCENARIO_NETWORK}"
  SCENARIO_IMAGE=$(bg_build_image)
  mkdir -p "${SCENARIO_ROOT}/config" "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/admission-books" "${SCENARIO_ROOT}/bindery-state"
  chown -R 99:100 "${SCENARIO_ROOT}"

  docker run --rm --user 99:100 \
    -e CONFIG_DIR=/config \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${SCENARIO_ROOT}/admission-books:/admission-books:rw" \
    -v "${SCENARIO_ROOT}/bindery-state:/bindery:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_publication_proof_fixture.py:/app/seed_publication_proof_fixture.py:ro" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_registration_conflict_fixture.py:/app/seed_registration_conflict_fixture.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_publication_proof_fixture.py
  staged_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/${RELATIVE}")

  printf '%s\n' '{"settings":{"import.mode":"external","import.drop_folder":"/data/bookguard-staging","autoGrab.enabled":"false"},"queue":[],"partial":false,"scanAttempts":0}' \
    > "${SCENARIO_ROOT}/bindery-state/state.json"
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"
  docker run -d --name "${SCENARIO_FAKE}" --network "${SCENARIO_NETWORK}" \
    --user 99:100 --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/fake_bindery_registration_conflict.py:/acceptance/fake.py:ro" \
    -e FAKE_BINDERY_STATE=/state/state.json \
    -e FAKE_BINDERY_DB=/state/bindery.db \
    -e FAKE_BINDERY_BOOK_ID=101 \
    -e FAKE_BINDERY_STORED_PATH="/data/media/books/${RELATIVE}" \
    -e PORT=8787 "${SCENARIO_IMAGE}" python /acceptance/fake.py >/dev/null

  bg_header "OBSERVE: VERIFIED FAILURE PLAN"
  start_bookguard observe ""
  observed=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run")
  bg_assert_contains "${observed}" '"planKind":"RECOVER_ADMISSION_PUBLICATION"' "exact failure plan"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EMPTY ALLOWLIST: NO FILESYSTEM PROOF"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/blocked.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "409" "${status}" "empty allowlist HTTP"
  bg_assert_contains "$(cat "${response}")" 'ACTION_NOT_ALLOWLISTED' "empty allowlist"
  [[ ! -e "${SCENARIO_ROOT}/admission-books/${RELATIVE}" ]] || bg_die "ebook was published"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT ALLOWLIST: DISPOSABLE PROOF ONLY"
  start_bookguard automatic "prove_supported_no_replace_method"
  response="${SCENARIO_ROOT}/proof.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "proof HTTP"
  bg_assert_contains "$(cat "${response}")" '"admissionPublished":false' "no ebook published"
  bg_assert_contains "$(cat "${response}")" '"libraryBytesChanged":false' "no library bytes changed"
  bg_assert_hash "${SCENARIO_ROOT}/staging/${RELATIVE}" "${staged_hash}" "staged bytes"
  [[ ! -e "${SCENARIO_ROOT}/admission-books/${RELATIVE}" ]] || bg_die "ebook was published"
  response="${SCENARIO_ROOT}/paused.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "post-proof HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "publication stays disabled"
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"
  bg_note "Disposable no-overwrite proof succeeded; ebook publication remains disabled."
}
