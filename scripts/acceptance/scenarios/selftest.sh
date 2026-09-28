#!/usr/bin/env bash

SELFTEST_TEMP=""

scenario_description() {
  printf '%s\n' "Validate acceptance harness safety helpers without Docker or network access."
}

scenario_requires_docker() {
  return 1
}

scenario_requires_acceptance_root() {
  return 1
}

scenario_cleanup() {
  if [[ -n "${SELFTEST_TEMP}" && -d "${SELFTEST_TEMP}" ]]; then
    rm -rf -- "${SELFTEST_TEMP}"
  fi
}

scenario_run() {
  bg_header "CREATE DISPOSABLE SELFTEST ROOT"
  SELFTEST_TEMP=$(mktemp -d -t bookguard-acceptance-selftest-XXXXXX)
  export BOOKGUARD_ACCEPTANCE_ROOT="${SELFTEST_TEMP}/bookguard-acceptance"

  local root scenario_root fixture original_hash actual_head safe_name
  local fake_bin captured_image
  root=$(bg_prepare_acceptance_root)
  [[ -f "${root}/.bookguard-acceptance-root" ]] || bg_die "Acceptance root marker was not created."

  bg_header "VERIFY DESTRUCTIVE-ROOT GUARDS"
  if (
    export BOOKGUARD_ACCEPTANCE_ROOT="/"
    bg_prepare_acceptance_root >/dev/null 2>&1
  ); then
    bg_die "Harness accepted / as a disposable root."
  fi

  if (
    export BOOKGUARD_ACCEPTANCE_ROOT="${BOOKGUARD_PRODUCTION_ROOT:-/mnt/cache/appdata/bookguard}/acceptance"
    bg_prepare_acceptance_root >/dev/null 2>&1
  ); then
    bg_die "Harness accepted a root inside the protected production root."
  fi

  bg_header "VERIFY SCENARIO RESET IS CONTAINED"
  scenario_root=$(bg_reset_scenario_root selftest)
  [[ "${scenario_root}" == "${root}/selftest" ]] || bg_die "Unexpected scenario root: ${scenario_root}"
  [[ -f "${scenario_root}/.bookguard-acceptance-scenario" ]] || bg_die "Scenario marker was not created."

  fixture="${scenario_root}/fixture.txt"
  printf '%s\n' "bookguard acceptance fixture" > "${fixture}"
  original_hash=$(bg_sha256 "${fixture}")
  bg_assert_hash "${fixture}" "${original_hash}" "selftest fixture"

  printf '%s\n' "mutation" >> "${fixture}"
  if bg_assert_hash "${fixture}" "${original_hash}" "mutated selftest fixture" >/dev/null 2>&1; then
    bg_die "Hash assertion failed to detect a modified fixture."
  fi

  bg_header "VERIFY EXACT-HEAD GUARD"
  actual_head=$(bg_repo_head)
  (
    export BOOKGUARD_ACCEPTANCE_EXPECTED_SHA="${actual_head}"
    bg_assert_expected_head
  )
  if (
    export BOOKGUARD_ACCEPTANCE_EXPECTED_SHA="0000000000000000000000000000000000000000"
    bg_assert_expected_head >/dev/null 2>&1
  ); then
    bg_die "Exact-head guard accepted the wrong commit."
  fi

  bg_header "VERIFY DOCKER NAME NAMESPACE"
  safe_name=$(bg_container_name "Finalization / Crash" "BookGuard")
  bg_assert_eq \
    "bookguard-acceptance-finalization-crash-bookguard" \
    "${safe_name}" \
    "sanitized Docker name"

  bg_header "VERIFY IMAGE BUILD OUTPUT CONTRACT"
  fake_bin="${SELFTEST_TEMP}/fake-bin"
  mkdir -p -- "${fake_bin}"
  cat > "${fake_bin}/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "simulated docker build progress"
exit 0
EOF
  chmod +x "${fake_bin}/docker"
  captured_image=$(
    export PATH="${fake_bin}:${PATH}"
    export BOOKGUARD_ACCEPTANCE_IMAGE="bookguard:acceptance-selftest"
    bg_build_image
  )
  bg_assert_eq \
    "bookguard:acceptance-selftest" \
    "${captured_image}" \
    "captured image reference"

  bg_note "All harness self-tests passed."
}
