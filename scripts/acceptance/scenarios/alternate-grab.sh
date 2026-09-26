#!/usr/bin/env bash

# Reuse the disposable app lifecycle and request helpers.
# shellcheck source=acquisition-progress.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/acquisition-progress.sh"

scenario_description() {
  printf '%s\n' "Grab one disposable alternate, then prove interrupted queue and verified-byte outcomes without replay."
}

setup_alternate() {
  SCENARIO_ROOT=$(bg_reset_scenario_root "${BG_ACCEPTANCE_SCENARIO}")
  SCENARIO_APP=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" app)
  SCENARIO_FAKE=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" bindery)
  SCENARIO_NETWORK=$(bg_container_name "${BG_ACCEPTANCE_SCENARIO}" net)
  bg_create_network "${SCENARIO_NETWORK}"
  SCENARIO_IMAGE=$(bg_build_image)
  mkdir -p "${SCENARIO_ROOT}/config" "${SCENARIO_ROOT}/staging" \
    "${SCENARIO_ROOT}/books" "${SCENARIO_ROOT}/bindery-state"
  chown -R 99:100 "${SCENARIO_ROOT}"
  docker run --rm --user 99:100 -e CONFIG_DIR=/config \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_alternate_fixture.py:/app/seed_alternate.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_alternate.py
  printf '%s\n' '{"queue":[],"grabAttempts":0}' \
    > "${SCENARIO_ROOT}/bindery-state/state.json"
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"
  docker run -d --name "${SCENARIO_FAKE}" --network "${SCENARIO_NETWORK}" \
    --user 99:100 --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/fake_bindery_alternate.py:/acceptance/fake.py:ro" \
    -e FAKE_BINDERY_STATE=/state/state.json -e PORT=8787 \
    "${SCENARIO_IMAGE}" python /acceptance/fake.py >/dev/null
}

observe_and_select() {
  local observed plan_id selected
  start_bookguard observe ""
  observed=$(observe_once)
  bg_assert_contains "${observed}" '"planKind":"SELECT_ALTERNATE_REPLACEMENT"' "exact plan"
  plan_id=$(docker exec "${SCENARIO_APP}" python -c '
from app.recovery_planner import recovery_plan_snapshot
plans = [p for p in recovery_plan_snapshot(100)["items"] if p["planKind"] == "SELECT_ALTERNATE_REPLACEMENT"]
assert len(plans) == 1
print(plans[0]["id"])
')
  selected=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"candidateGuid":"alternate-guid","confirm":"SELECT_ALTERNATE_CANDIDATE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/plans/${plan_id}/alternate-selection")
  bg_assert_contains "${selected}" '"liveGrabEnabled":false' "selection is local only"
  bg_assert_contains "${selected}" '"currentPlan":true' "selection bound to current plan"
  bg_assert_contains "$(cat "${SCENARIO_ROOT}/bindery-state/state.json")" '"grabAttempts":0' "observe no grab"
  bg_remove_container "${SCENARIO_APP}"
}

advance_to_grab_without_allowlist() {
  local response status
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/blocked.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "409" "${status}" "empty allowlist HTTP"
  bg_assert_contains "$(cat "${response}")" 'ACTION_NOT_ALLOWLISTED' "no alternate grab"
  bg_assert_contains "$(cat "${SCENARIO_ROOT}/bindery-state/state.json")" '"grabAttempts":0' "empty allowlist state"
  bg_remove_container "${SCENARIO_APP}"
}

seed_running_child() {
  docker run --rm --user 99:100 \
    -e CONFIG_DIR=/config \
    -e BOOKGUARD_ALLOW_ACTIONS=true \
    -e BOOKGUARD_AUTOMATION_MODE=automatic \
    -e BOOKGUARD_AUTOMATIC_REACQUISITION=true \
    -e BOOKGUARD_STAGING_ROOT=/staging \
    -e BOOKGUARD_BINDERY_DROP_FOLDER=/data/bookguard-staging \
    -e BINDERY_URL="http://${SCENARIO_FAKE}:8787" \
    -e BINDERY_API_KEY=acceptance \
    -e EBOOK_ROOT=/books -e EBOOK_BINDERY_PREFIX=/data/media/books \
    --network "${SCENARIO_NETWORK}" \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${SCENARIO_ROOT}/books:/books:ro" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_alternate_running.py:/app/seed_running.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_running.py
}

seed_verified_child() {
  docker run --rm --user 99:100 \
    -e CONFIG_DIR=/config \
    -e BOOKGUARD_STAGING_ROOT=/staging \
    -e BINDERY_URL="http://${SCENARIO_FAKE}:8787" \
    -e BINDERY_API_KEY=acceptance \
    --network "${SCENARIO_NETWORK}" \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_alternate_verified.py:/app/seed_verified.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_verified.py
}

set_verified_queue() {
  printf '%s\n' '{"queue":[{"id":77,"bookId":101,"title":"Fixture Author - Alternate Fixture alternate epub","protocol":"usenet","status":"importExternal"}],"grabAttempts":0}' \
    > "${SCENARIO_ROOT}/bindery-state/state.json"
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"
}

assert_no_media() {
  [[ -z "$(find "${SCENARIO_ROOT}/books" -type f -print -quit)" ]] ||
    bg_die "Alternate scenario published an ebook."
  [[ -z "$(find "${SCENARIO_ROOT}/staging" -type f -print -quit)" ]] ||
    bg_die "Alternate scenario created staged bytes."
}

scenario_run() {
  local response status executions
  setup_alternate
  bg_header "OBSERVE, CHOOSE, EMPTY ALLOWLIST"
  observe_and_select
  advance_to_grab_without_allowlist

  bg_header "EXACT ALLOWLIST: ONE DISPOSABLE GRAB"
  start_bookguard automatic "request_alternate_grab"
  response="${SCENARIO_ROOT}/grabbed.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "live grab HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "live grab outcome"
  bg_assert_contains "$(cat "${response}")" '"admissionAttempted":false' "admission still gated"
  bg_assert_contains "$(cat "${SCENARIO_ROOT}/bindery-state/state.json")" '"grabAttempts":1' "one grab"
  docker exec "${SCENARIO_APP}" python -c '
from app.db import ebook_replacement_for_acquisition
child = ebook_replacement_for_acquisition(1)
assert child and child["status"] == "queued" and child["queue_id"] == 77
assert child["grab_response"]["id"] == 77
assert child["admission_id"] is None
' || bg_die "The accepted grab lacks a proven durable queue identity."
  assert_no_media
  response="${SCENARIO_ROOT}/replay.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "post-success cycle"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "no second grab"
  bg_assert_contains "$(cat "${SCENARIO_ROOT}/bindery-state/state.json")" '"grabAttempts":1' "replay refused"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard observe ""
  response=$(observe_once)
  bg_assert_contains "${response}" '"planKind":"RECONCILE_ACQUISITION"' "linked child observed"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard automatic "request_alternate_grab,resume_known_transition"
  response="${SCENARIO_ROOT}/handoff.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "child handoff HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"waiting"' "child waits for verified staging"
  bg_assert_contains "$(cat "${response}")" '"planKind":"RECONCILE_ACQUISITION"' "parent did not starve child"
  bg_assert_contains "$(cat "${SCENARIO_ROOT}/bindery-state/state.json")" '"grabAttempts":1' "handoff no duplicate grab"
  assert_no_media
  scenario_cleanup

  bg_header "INTERRUPTED GRAB: PROVE QUEUE WITHOUT REPLAY"
  setup_alternate
  observe_and_select
  advance_to_grab_without_allowlist
  seed_running_child
  printf '%s\n' '{"queue":[{"id":77,"bookId":101,"title":"Fixture Author - Alternate Fixture alternate epub","protocol":"usenet","status":"downloading"}],"grabAttempts":0}' \
    > "${SCENARIO_ROOT}/bindery-state/state.json"
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"
  start_bookguard automatic "request_alternate_grab"
  response="${SCENARIO_ROOT}/adopted.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "interrupted adoption HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"reconciled"' "queue proof adopted"
  bg_assert_contains "$(cat "${response}")" '"admissionAttempted":false' "no admission"
  bg_assert_contains "$(cat "${SCENARIO_ROOT}/bindery-state/state.json")" '"grabAttempts":0' "no second grab"
  executions=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/executions")
  bg_assert_contains "${executions}" '"actionCode":"request_alternate_grab"' "execution journal"
  assert_no_media
  bg_note "Disposable alternate grab and interrupted queue adoption passed without admission."
  scenario_cleanup

  bg_header "INTERRUPTED VERIFIED CHILD: PROVE DURABLE BYTES WITHOUT REPLAY"
  setup_alternate
  observe_and_select
  advance_to_grab_without_allowlist
  seed_running_child
  set_verified_queue
  seed_verified_child
  local verified_hash
  verified_hash=$(bg_sha256 "${SCENARIO_ROOT}/staging/Alternate Fixture.epub")
  start_bookguard automatic "request_alternate_grab"
  response="${SCENARIO_ROOT}/verified-adopted.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "verified adoption HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"reconciled"' "verified child adopted"
  bg_assert_contains "$(cat "${response}")" '"admissionAttempted":false' "verified child not admitted"
  bg_assert_contains "$(cat "${SCENARIO_ROOT}/bindery-state/state.json")" '"grabAttempts":0' "verified child no grab replay"
  bg_assert_eq "${verified_hash}" "$(bg_sha256 "${SCENARIO_ROOT}/staging/Alternate Fixture.epub")" "verified bytes unchanged"
  [[ -z "$(find "${SCENARIO_ROOT}/books" -type f -print -quit)" ]] ||
    bg_die "Verified adoption published an ebook."
  docker exec "${SCENARIO_APP}" python -c '
from app.db import ebook_replacement_for_acquisition
child = ebook_replacement_for_acquisition(1)
assert child and child["status"] == "verified" and child["queue_id"] == 77
assert child["verification"]["safeToAdmit"] is True
assert child["staged_sha256"] == child["verification"]["sha256"]
assert child["admission_id"] is None
' || bg_die "Verified child lacks exact durable staged proof."
  scenario_cleanup

  bg_header "CHANGED VERIFIED BYTES: BLOCK WITHOUT GRAB OR ADMISSION"
  setup_alternate
  observe_and_select
  advance_to_grab_without_allowlist
  seed_running_child
  set_verified_queue
  seed_verified_child
  printf '%s' changed >> "${SCENARIO_ROOT}/staging/Alternate Fixture.epub"
  start_bookguard automatic "request_alternate_grab"
  response="${SCENARIO_ROOT}/verified-changed.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "changed bytes HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"blocked"' "changed verified child blocked"
  bg_assert_contains "$(cat "${response}")" 'UNCERTAIN_EXTERNAL_OUTCOME' "changed bytes lack exact proof"
  bg_assert_contains "$(cat "${SCENARIO_ROOT}/bindery-state/state.json")" '"grabAttempts":0' "changed bytes no grab replay"
  [[ -z "$(find "${SCENARIO_ROOT}/books" -type f -print -quit)" ]] ||
    bg_die "Changed bytes published an ebook."
  docker exec "${SCENARIO_APP}" python -c '
from app.db import ebook_replacement_for_acquisition
child = ebook_replacement_for_acquisition(1)
assert child and child["admission_id"] is None
' || bg_die "Changed verified bytes reached admission."
  bg_note "Verified restart adopted exact disposable bytes and blocked changed bytes without replay."
}
