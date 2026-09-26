#!/usr/bin/env bash

# Reuse the disposable progress fixture and app lifecycle.
# shellcheck source=acquisition-progress.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/acquisition-progress.sh"

scenario_description() {
  printf '%s\n' "Adopt an interrupted disposable staging observation only with a current exact Bindery queue."
}

seed_running_observation() {
  docker run --rm --user 99:100 \
    --network "${SCENARIO_NETWORK}" \
    -e CONFIG_DIR=/config \
    -e BOOKGUARD_ALLOW_ACTIONS=true \
    -e BOOKGUARD_AUTOMATION_MODE=observe \
    -e BOOKGUARD_AUTOMATIC_REACQUISITION=true \
    -e BOOKGUARD_STAGING_ROOT=/staging \
    -e BOOKGUARD_BINDERY_DROP_FOLDER=/data/bookguard-staging \
    -e BINDERY_URL="http://${SCENARIO_FAKE}:8787" \
    -e BINDERY_API_KEY=acceptance \
    -e EBOOK_ROOT=/books \
    -e EBOOK_BINDERY_PREFIX=/data/media/books \
    -v "${SCENARIO_ROOT}/config:/config:rw" \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${SCENARIO_ROOT}/books:/books:ro" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_acquisition_progress_running.py:/app/seed_running.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_running.py
}

assert_disposable_unchanged() {
  bg_assert_hash "${SCENARIO_ROOT}/staging/Progress Fixture.epub" \
    "${SCENARIO_FIXTURE_HASH}" "staged bytes"
  [[ ! -e "${SCENARIO_ROOT}/books/Progress Fixture.epub" ]] ||
    bg_die "Interrupted progression unexpectedly published an ebook."
}

scenario_run() {
  local response status

  bg_header "INTERRUPTED OBSERVATION: EXACT QUEUE ADOPTION"
  setup_progress_fixture
  start_bookguard observe ""
  bg_assert_contains "$(observe_once)" '"planKind":"RECONCILE_ACQUISITION"' "planned observation"
  bg_remove_container "${SCENARIO_APP}"
  seed_running_observation
  start_bookguard automatic "resume_known_transition"
  response="${SCENARIO_ROOT}/adopted.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "adoption HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"reconciled"' "adopted once"
  bg_assert_contains "$(cat "${response}")" '"status":"staging_observed"' "durable status"
  assert_disposable_unchanged
  bg_remove_container "${SCENARIO_APP}"
  scenario_cleanup

  bg_header "INTERRUPTED OBSERVATION: CONTRADICTORY QUEUE BLOCKS"
  setup_progress_fixture
  start_bookguard observe ""
  bg_assert_contains "$(observe_once)" '"planKind":"RECONCILE_ACQUISITION"' "planned observation"
  bg_remove_container "${SCENARIO_APP}"
  seed_running_observation
  cat > "${SCENARIO_ROOT}/bindery-state/state.json" <<'JSON'
{"queue":[{"id":77,"queueId":78,"bookId":101,"title":"Progress Fixture release","protocol":"usenet","status":"importExternal"}],"partial":false,"queueStatus":200}
JSON
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"
  start_bookguard automatic "resume_known_transition"
  response="${SCENARIO_ROOT}/blocked.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "blocked outcome HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"blocked"' "queue conflict blocked"
  bg_assert_contains "$(cat "${response}")" 'UNCERTAIN_EXTERNAL_OUTCOME' "uncertain outcome"
  docker exec "${SCENARIO_APP}" python -c '
from app.db import ebook_acquisition_by_id
item = ebook_acquisition_by_id(1)
assert item["status"] == "staging_observed" and item["admission_id"] is None
' || bg_die "The blocked acquisition changed state or attempted admission."
  assert_disposable_unchanged
  bg_note "Interrupted observation adopted only with an exact queue; contradiction blocked without replay."
}
