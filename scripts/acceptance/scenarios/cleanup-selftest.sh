#!/usr/bin/env bash

SELFTEST_TEMP=""

scenario_description() {
  printf '%s\n' "Prove marked disposable cleanup never follows links or removes siblings."
}

scenario_requires_docker() { return 1; }
scenario_requires_acceptance_root() { return 1; }

scenario_cleanup() {
  if [[ "${BOOKGUARD_ACCEPTANCE_TEST_CLEANUP_FAILURE:-}" == "1" ]]; then
    return 1
  fi
  [[ -z "${SELFTEST_TEMP}" ]] || rm -rf -- "${SELFTEST_TEMP}"
}

scenario_run() {
  local root scenario sibling linked child_output
  if [[ "${BOOKGUARD_ACCEPTANCE_TEST_CLEANUP_FAILURE:-}" == "1" ]]; then
    return 0
  fi
  SELFTEST_TEMP=$(mktemp -d -t bookguard-acceptance-cleanup-XXXXXX)
  export BOOKGUARD_ACCEPTANCE_ROOT="${SELFTEST_TEMP}/bookguard-acceptance"
  root=$(bg_prepare_acceptance_root)
  sibling="${SELFTEST_TEMP}/important-sibling"
  mkdir -p "${sibling}"
  printf '%s\n' "preserve this" > "${sibling}/keep.txt"

  scenario=$(bg_reset_scenario_root cleanup-selftest)
  printf '%s\n' "disposable" > "${scenario}/discard.txt"
  bg_cleanup_scenario_root cleanup-selftest
  [[ ! -e "${scenario}" ]] || bg_die "Marked scenario root survived cleanup."
  [[ -f "${root}/.bookguard-acceptance-root" ]] || bg_die "Root marker was removed."
  [[ -f "${sibling}/keep.txt" ]] || bg_die "Sibling was removed."

  mkdir -p "${scenario}"
  printf '%s\n' "unmarked" > "${scenario}/keep.txt"
  if bg_cleanup_scenario_root cleanup-selftest >/dev/null 2>&1; then
    bg_die "Cleanup accepted an unmarked scenario directory."
  fi
  [[ -f "${scenario}/keep.txt" ]] || bg_die "Unmarked directory was removed."
  if bg_reset_scenario_root cleanup-selftest >/dev/null 2>&1; then
    bg_die "Scenario reset accepted an unmarked directory."
  fi
  [[ -f "${scenario}/keep.txt" ]] || bg_die "Scenario reset removed an unmarked directory."
  rm -rf -- "${scenario}"

  ln -s "${sibling}" "${scenario}"
  if bg_cleanup_scenario_root cleanup-selftest >/dev/null 2>&1; then
    bg_die "Cleanup accepted a scenario symlink."
  fi
  [[ -f "${sibling}/keep.txt" ]] || bg_die "Symlink target was removed."
  rm -- "${scenario}"

  linked="${SELFTEST_TEMP}/linked-acceptance"
  ln -s "${root}" "${linked}"
  if (
    export BOOKGUARD_ACCEPTANCE_ROOT="${linked}"
    bg_prepare_acceptance_root >/dev/null 2>&1
  ); then
    bg_die "Acceptance root symlink was accepted."
  fi
  if bg_cleanup_scenario_root '../important-sibling' >/dev/null 2>&1; then
    bg_die "Cleanup accepted an escaping scenario name."
  fi
  mkdir -p "${SELFTEST_TEMP}/unmarked-acceptance"
  if (
    export BOOKGUARD_ACCEPTANCE_ROOT="${SELFTEST_TEMP}/unmarked-acceptance"
    bg_prepare_acceptance_root >/dev/null 2>&1
  ); then
    bg_die "Existing unmarked acceptance root was accepted."
  fi
  [[ -f "${sibling}/keep.txt" ]] || bg_die "Sibling changed during guard tests."
  if child_output=$(BOOKGUARD_ACCEPTANCE_TEST_CLEANUP_FAILURE=1 \
      bash "${BG_ACCEPTANCE_REPO_ROOT}/scripts/acceptance/run" cleanup-selftest 2>&1); then
    bg_die "A failed scenario cleanup was reported as a pass."
  fi
  bg_assert_contains "${child_output}" "Scenario cleanup reported failure." \
    "cleanup failure explanation"
  bg_assert_contains "${child_output}" "FAIL: cleanup-selftest" \
    "cleanup failure status"
  bg_note "Marked scenario cleanup removed only its own disposable directory."
}
