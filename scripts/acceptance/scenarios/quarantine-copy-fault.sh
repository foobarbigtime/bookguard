#!/usr/bin/env bash

SCENARIO_ROOT=""
SCENARIO_IMAGE=""

scenario_description() {
  printf '%s\n' "Corrupt a disposable cross-mount quarantine copy and preserve source and control bytes."
}

scenario_requires_docker() { return 0; }
scenario_requires_acceptance_root() { return 0; }
scenario_cleanup() { return 0; }

scenario_run() {
  local result
  SCENARIO_ROOT=$(bg_reset_scenario_root "${BG_ACCEPTANCE_SCENARIO}")
  SCENARIO_IMAGE=$(bg_build_image)
  mkdir -p -- "${SCENARIO_ROOT}/books" "${SCENARIO_ROOT}/quarantine"
  chown -R 99:100 -- "${SCENARIO_ROOT}"

  result=$(docker run --rm --network none --user 99:100 --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m \
    -v "${SCENARIO_ROOT}/books:/books:rw" \
    -v "${SCENARIO_ROOT}/quarantine:/quarantine:rw" \
    -v "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/services/prove_quarantine_copy_fault.py:/app/proof.py:ro" \
    "${SCENARIO_IMAGE}" python /app/proof.py)
  bg_assert_contains "${result}" '"corruptCopyRefused": true' "corrupt cross-mount copy"
  bg_assert_contains "${result}" '"sourceRestored": true' "verified rollback"
  bg_assert_contains "${result}" '"controlUnchanged": true' "unrelated bytes"
  bg_note "Corrupt private copy was refused; exact source and unrelated bytes survived."
}
