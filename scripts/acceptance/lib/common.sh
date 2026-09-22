#!/usr/bin/env bash

bg_header() {
  printf '\n===== %s =====\n' "$1"
}

bg_note() {
  printf '%s\n' "$*"
}

bg_die() {
  printf 'ERROR: %s\n' "$*" >&2
  return 1
}

bg_require_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "${cmd}" >/dev/null 2>&1 || bg_die "Required command not found: ${cmd}"
  done
}

bg_repo_head() {
  bg_require_cmd git
  git -C "${BG_ACCEPTANCE_REPO_ROOT}" rev-parse HEAD
}

bg_repo_short_head() {
  bg_require_cmd git
  git -C "${BG_ACCEPTANCE_REPO_ROOT}" rev-parse --short=7 HEAD
}

bg_assert_expected_head() {
  local expected=${BOOKGUARD_ACCEPTANCE_EXPECTED_SHA:-}
  [[ -n "${expected}" ]] || return 0
  local actual
  actual=$(bg_repo_head)
  [[ "${actual}" == "${expected}" ]] || bg_die "HEAD ${actual} does not match BOOKGUARD_ACCEPTANCE_EXPECTED_SHA=${expected}."
}

bg_acceptance_root() {
  printf '%s\n' "${BOOKGUARD_ACCEPTANCE_ROOT:-${BG_ACCEPTANCE_REPO_ROOT}/.acceptance}"
}

bg_assert_safe_acceptance_root() {
  local root production basename_root
  root=$(bg_acceptance_root)
  production=${BOOKGUARD_PRODUCTION_ROOT:-/mnt/cache/appdata/bookguard}
  basename_root=$(basename -- "${root}")

  [[ "${root}" == /* ]] || bg_die "BOOKGUARD_ACCEPTANCE_ROOT must be an absolute path: ${root}"
  [[ "${root}" != "/" ]] || bg_die "Refusing to use / as an acceptance root."
  [[ "${root}" != *"/../"* && "${root}" != */.. ]] || bg_die "Acceptance root must not contain '..': ${root}"
  [[ "${root}" != "${BG_ACCEPTANCE_REPO_ROOT}" ]] || bg_die "Acceptance root must not be the repository root."
  [[ "${basename_root}" == *acceptance* ]] || bg_die "Acceptance root basename must contain 'acceptance': ${root}"

  case "${root}/" in
    "${production}/"*)
      bg_die "Acceptance root must not be production or live beneath it: ${root}"
      ;;
  esac
}

bg_prepare_acceptance_root() {
  local root
  bg_assert_safe_acceptance_root
  root=$(bg_acceptance_root)
  mkdir -p -- "${root}"
  printf '%s\n' "BookGuard disposable acceptance root" > "${root}/.bookguard-acceptance-root"
  printf '%s\n' "${root}"
}

bg_reset_scenario_root() {
  local scenario=$1 root target
  [[ "${scenario}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || bg_die "Unsafe scenario name: ${scenario}"
  root=$(bg_prepare_acceptance_root)
  [[ -f "${root}/.bookguard-acceptance-root" ]] || bg_die "Acceptance root marker is missing: ${root}"
  target="${root}/${scenario}"
  case "${target}/" in
    "${root}/"*) ;;
    *) bg_die "Scenario root escaped acceptance root: ${target}" ;;
  esac
  rm -rf -- "${target}"
  mkdir -p -- "${target}"
  printf '%s\n' "BookGuard disposable scenario root" > "${target}/.bookguard-acceptance-scenario"
  printf '%s\n' "${target}"
}

bg_sha256() {
  local file=$1
  bg_require_cmd sha256sum awk
  [[ -f "${file}" ]] || bg_die "Cannot hash missing file: ${file}"
  sha256sum -- "${file}" | awk '{print $1}'
}

bg_assert_hash() {
  local file=$1 expected=$2 label=${3:-$1} actual
  actual=$(bg_sha256 "${file}")
  [[ "${actual}" == "${expected}" ]] || bg_die "Hash changed for ${label}: expected ${expected}, got ${actual}."
}

bg_assert_eq() {
  local expected=$1 actual=$2 label=${3:-value}
  [[ "${actual}" == "${expected}" ]] || bg_die "Unexpected ${label}: expected '${expected}', got '${actual}'."
}

bg_assert_contains() {
  local haystack=$1 needle=$2 label=${3:-value}
  [[ "${haystack}" == *"${needle}"* ]] || bg_die "${label} did not contain '${needle}'."
}

bg_container_name() {
  local scenario=$1 suffix=${2:-} raw safe
  raw="${scenario}${suffix:+-${suffix}}"
  safe=$(printf '%s' "${raw}" |
    tr '[:upper:]' '[:lower:]' |
    sed -E 's/[^a-z0-9_.-]+/-/g; s/^-+//; s/-+$//')
  [[ -n "${safe}" ]] || bg_die "Could not derive a safe Docker name from '${raw}'."
  printf 'bookguard-acceptance-%s\n' "${safe}"
}

bg_remove_container() {
  local name=$1
  docker rm -f "${name}" >/dev/null 2>&1 || true
}

bg_remove_network() {
  local name=$1
  docker network rm "${name}" >/dev/null 2>&1 || true
}

bg_create_network() {
  local name=$1
  bg_remove_network "${name}"
  docker network create "${name}" >/dev/null
}

bg_wait_http() {
  local url=$1 attempts=${2:-30} delay=${3:-1} i
  bg_require_cmd curl
  for i in $(seq 1 "${attempts}"); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep "${delay}"
  done
  bg_die "Timed out waiting for ${url}."
}

bg_build_image() {
  bg_require_cmd docker git
  bg_assert_expected_head
  local image
  image=${BOOKGUARD_ACCEPTANCE_IMAGE:-bookguard:acceptance-$(bg_repo_short_head)}
  bg_header "BUILD ${image}"
  docker build --tag "${image}" "${BG_ACCEPTANCE_REPO_ROOT}"
  export BG_ACCEPTANCE_IMAGE_RESOLVED="${image}"
  printf '%s\n' "${image}"
}
