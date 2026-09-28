#!/usr/bin/env bash

# shellcheck source=quarantine-grab.sh
source "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/scenarios/quarantine-grab.sh"

SCENARIO_PORT=${BOOKGUARD_QUARANTINE_HANDOFF_PORT:-8813}

scenario_description() {
  printf '%s\n' "Prove a quarantined replacement reaches verified staging without admission."
}

observe_handoff() {
  curl -fsS -u "${AUTH_USER}:${AUTH_PASSWORD}" \
    -H 'Content-Type: application/json' \
    -d '{"confirm":"RUN_OBSERVE_MODE"}' \
    "http://127.0.0.1:${SCENARIO_PORT}/api/automatic/observe/run"
}

setup_verified_handoff() {
  local response status observed
  setup_and_grab
  bg_remove_container "${SCENARIO_APP}"

  docker run --rm -i --user 99:100 \
    -v "${SCENARIO_ROOT}/bindery-state:/state:rw" \
    "${SCENARIO_IMAGE}" python - /state/state.json <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
state = json.loads(path.read_text())
assert state["grabAttempts"] == 1 and len(state["queue"]) == 1
assert state["queue"][0]["id"] == 77
state["queue"][0]["status"] = "importExternal"
path.write_text(json.dumps(state, sort_keys=True))
PY
  docker run --rm --user 99:100 \
    -v "${SCENARIO_ROOT}/staging:/staging:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/seed_quarantine_handoff.py:/app/seed_handoff.py:ro" \
    "${SCENARIO_IMAGE}" python /app/seed_handoff.py
  SCENARIO_STAGED_HASH=$(bg_sha256 "${SCENARIO_ROOT}/staging/Conflict Fixture.epub")

  bg_header "OBSERVE LINKED CHILD"
  start_bookguard observe ""
  observed=$(observe_handoff)
  bg_assert_contains "${observed}" '"planKind":"RECONCILE_ACQUISITION"' "child plan"
  bg_assert_contains "${observed}" \
    "\"replacementForQuarantinePlanId\":${SCENARIO_PLAN_ID}" "quarantine lineage"
  bg_remove_container "${SCENARIO_APP}"

  bg_header "OBSERVE THEN VERIFY STAGED REPLACEMENT"
  start_bookguard automatic "resume_known_transition"
  response="${SCENARIO_ROOT}/observed-staging.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "first progression HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"staging_observed"' "staging observed"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard observe ""
  observed=$(observe_handoff)
  bg_assert_contains "${observed}" '"planKind":"RECONCILE_ACQUISITION"' "new staged plan"
  bg_remove_container "${SCENARIO_APP}"
  start_bookguard automatic "resume_known_transition"
  response="${SCENARIO_ROOT}/verified.json"
  status=$(post_cycle "${response}")
  bg_assert_eq "200" "${status}" "verification HTTP"
  bg_assert_contains "$(cat "${response}")" '"status":"verified"' "verified child"
  bg_assert_contains "$(cat "${response}")" '"admissionAttempted":false' "admission gated"
  docker exec "${SCENARIO_APP}" python -c '
from app.acquisition_progress import _verified_snapshot_matches
from app.db import ebook_replacement_for_quarantine_plan
from app.staging import list_staged_ebooks
child = ebook_replacement_for_quarantine_plan(1)
items = list_staged_ebooks(1000)["items"]
assert child and child["status"] == "verified" and child["admission_id"] is None
assert len(items) == 1
item = items[0]
assert _verified_snapshot_matches(
    child, 101, (item["relativePath"], item["size"], item["modifiedNs"]),
    child["staged_sha256"],
)
'
}

scenario_run() {
  local state
  setup_verified_handoff
  bg_assert_hash "${SCENARIO_ROOT}/staging/Conflict Fixture.epub" \
    "${SCENARIO_STAGED_HASH}" "verified staged bytes"
  bg_assert_hash "${SCENARIO_ROOT}/books/Other/Unrelated.epub" \
    "${SCENARIO_SIBLING_HASH}" "unrelated media"
  [[ ! -e "${SCENARIO_ROOT}/books/${RELATIVE}" ]] || bg_die "Unsafe source was restored."
  assert_associations 0
  state=$(tr -d '[:space:]' < "${SCENARIO_ROOT}/bindery-state/state.json")
  bg_assert_contains "${state}" '"grabAttempts":1' "no second grab"
  bg_assert_contains "${state}" '"scanAttempts":0' "no Bindery scan"
  bg_note "Linked disposable replacement verified with no admission or second grab."
}
