#!/usr/bin/env bash

# shellcheck source=quarantine-candidate-selection.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/quarantine-candidate-selection.sh"

SCENARIO_PORT=${BOOKGUARD_QUARANTINE_GRAB_PORT:-8812}
SCENARIO_FAKE_SCRIPT=fake_bindery_quarantine_grab.py

scenario_description() {
  printf '%s\n' "Prove one disposable post-quarantine grab with no admission or scan."
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
    -e BOOKGUARD_AUTOMATIC_REACQUISITION=true \
    -e BOOKGUARD_STAGING_ROOT=/staging \
    -e BOOKGUARD_BINDERY_DROP_FOLDER=/data/bookguard-staging \
    -e BINDERY_URL="http://${SCENARIO_FAKE}:8787" \
    -e BINDERY_API_KEY=acceptance \
    -e BINDERY_DB=/bindery/bindery.db \
    -e EBOOK_ROOT=/books \
    -e EBOOK_BINDERY_PREFIX=/data/media/books \
    -e QUARANTINE_ROOT=/quarantine \
    "${SCENARIO_IMAGE}" >/dev/null
  bg_wait_http "http://127.0.0.1:${SCENARIO_PORT}/health" 45 1
}

scenario_run() {
  local response status plan_id selected sibling_hash state
  setup_disposable_quarantine
  mkdir -p "${SCENARIO_ROOT}/staging"
  chown 99:100 "${SCENARIO_ROOT}/staging"
  sibling_hash=$(bg_sha256 "${SCENARIO_ROOT}/books/Other/Unrelated.epub")
  python - "${SCENARIO_ROOT}/bindery-state/state.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
state = json.loads(path.read_text())
state["settings"] = {
    "import.mode": "external",
    "import.drop_folder": "/data/bookguard-staging",
    "import.drop_layout": "flat",
    "import.drop_link_mode": "copy",
    "autoGrab.enabled": "false",
}
path.write_text(json.dumps(state, sort_keys=True))
PY
  chown 99:100 "${SCENARIO_ROOT}/bindery-state/state.json"

  observe_plan
  start_bookguard automatic "quarantine_exact_media"
  response="${SCENARIO_ROOT}/quarantined.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "exact quarantine HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "exact quarantine"
  plan_id=$(docker exec "${SCENARIO_APP}" python -c '
from app.recovery_planner import recovery_plan_snapshot
plans = [p for p in recovery_plan_snapshot(100)["items"]
         if p["planKind"] == "QUARANTINE_UNSAFE_MEDIA"]
assert len(plans) == 1 and plans[0]["currentStep"] == 3
print(plans[0]["id"])
')
  selected=$(curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"candidateGuid":"quarantine-alternate-guid","confirm":"SELECT_QUARANTINE_REPLACEMENT_CANDIDATE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/plans/${plan_id}/quarantine-candidate-selection")
  bg_assert_contains "${selected}" '"currentPlan":true' "exact operator choice"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EMPTY ALLOWLIST: NO REPLACEMENT GRAB"
  start_bookguard automatic ""
  response="${SCENARIO_ROOT}/paused.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "paused HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "no implicit grab"
  bg_assert_contains "$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")" \
    '"grabAttempts":0' "no grab"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "EXACT ALLOWLIST: ONE DISPOSABLE GRAB"
  start_bookguard automatic "reacquire_expected_media"
  response="${SCENARIO_ROOT}/grabbed.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "replacement grab HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"executed"' "replacement requested"
  docker exec "${SCENARIO_APP}" python -c '
from app.db import ebook_replacement_for_quarantine_plan
child = ebook_replacement_for_quarantine_plan(1)
assert child and child["queue_id"] == 77 and child["status"] == "queued"
assert child["admission_id"] is None
'
  response="${SCENARIO_ROOT}/after-grab.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "post-grab HTTP"
  bg_assert_contains "$(cat "${response}")" '"state":"paused"' "later steps disabled"
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"grabAttempts":1' "one exact grab"
  bg_assert_contains "${state}" '"scanAttempts":0' "no scan"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" \
    "${sibling_hash}" "unrelated media"
  [[ ! -e "${SCENARIO_ROOT}/books/${RELATIVE}" ]] || bg_die "Unsafe source was restored."
  assert_associations 0
  bg_note "One disposable replacement queued; no ebook was admitted or scanned."
}
