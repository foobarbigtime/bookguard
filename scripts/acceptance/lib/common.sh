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
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      bg_die "Required command not found: ${cmd}"
      return 1
    fi
  done
}

bg_repo_head() {
  if ! bg_require_cmd git; then
    return 1
  fi
  git -C "${BG_ACCEPTANCE_REPO_ROOT}" rev-parse HEAD
}

bg_repo_short_head() {
  if ! bg_require_cmd git; then
    return 1
  fi
  git -C "${BG_ACCEPTANCE_REPO_ROOT}" rev-parse --short=7 HEAD
}

bg_assert_expected_head() {
  local expected=${BOOKGUARD_ACCEPTANCE_EXPECTED_SHA:-}
  [[ -n "${expected}" ]] || return 0
  local actual
  if ! actual=$(bg_repo_head); then
    return 1
  fi
  if [[ "${actual}" != "${expected}" ]]; then
    bg_die "HEAD ${actual} does not match BOOKGUARD_ACCEPTANCE_EXPECTED_SHA=${expected}."
    return 1
  fi
}

bg_acceptance_root() {
  printf '%s\n' "${BOOKGUARD_ACCEPTANCE_ROOT:-${BG_ACCEPTANCE_REPO_ROOT}/.acceptance}"
}

bg_assert_safe_acceptance_root() {
  local root production basename_root
  root=$(bg_acceptance_root)
  production=${BOOKGUARD_PRODUCTION_ROOT:-/mnt/cache/appdata/bookguard}
  basename_root=$(basename -- "${root}")

  if [[ "${root}" != /* ]]; then
    bg_die "BOOKGUARD_ACCEPTANCE_ROOT must be an absolute path: ${root}"
    return 1
  fi
  if [[ "${root}" == "/" ]]; then
    bg_die "Refusing to use / as an acceptance root."
    return 1
  fi
  if [[ "${root}" == *"/../"* || "${root}" == */.. ]]; then
    bg_die "Acceptance root must not contain '..': ${root}"
    return 1
  fi
  if [[ "${root}" == "${BG_ACCEPTANCE_REPO_ROOT}" ]]; then
    bg_die "Acceptance root must not be the repository root."
    return 1
  fi
  if [[ "${basename_root}" != *acceptance* ]]; then
    bg_die "Acceptance root basename must contain 'acceptance': ${root}"
    return 1
  fi

  case "${root}/" in
    "${production}/"*)
      bg_die "Acceptance root must not be production or live beneath it: ${root}"
      return 1
      ;;
  esac
}

bg_prepare_acceptance_root() {
  local root
  if ! bg_assert_safe_acceptance_root; then
    return 1
  fi
  root=$(bg_acceptance_root)
  if ! mkdir -p -- "${root}"; then
    bg_die "Could not create acceptance root: ${root}"
    return 1
  fi
  printf '%s\n' "BookGuard disposable acceptance root" > "${root}/.bookguard-acceptance-root"
  printf '%s\n' "${root}"
}

bg_reset_scenario_root() {
  local scenario=$1 root target
  if [[ ! "${scenario}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
    bg_die "Unsafe scenario name: ${scenario}"
    return 1
  fi
  if ! root=$(bg_prepare_acceptance_root); then
    return 1
  fi
  if [[ ! -f "${root}/.bookguard-acceptance-root" ]]; then
    bg_die "Acceptance root marker is missing: ${root}"
    return 1
  fi
  target="${root}/${scenario}"
  case "${target}/" in
    "${root}/"*) ;;
    *)
      bg_die "Scenario root escaped acceptance root: ${target}"
      return 1
      ;;
  esac
  rm -rf -- "${target}"
  mkdir -p -- "${target}"
  printf '%s\n' "BookGuard disposable scenario root" > "${target}/.bookguard-acceptance-scenario"
  printf '%s\n' "${target}"
}

bg_sha256() {
  local file=$1
  if ! bg_require_cmd sha256sum awk; then
    return 1
  fi
  if [[ ! -f "${file}" ]]; then
    bg_die "Cannot hash missing file: ${file}"
    return 1
  fi
  sha256sum -- "${file}" | awk '{print $1}'
}

bg_assert_hash() {
  local file=$1 expected=$2 label=${3:-$1} actual
  if ! actual=$(bg_sha256 "${file}"); then
    return 1
  fi
  if [[ "${actual}" != "${expected}" ]]; then
    bg_die "Hash changed for ${label}: expected ${expected}, got ${actual}."
    return 1
  fi
}

bg_assert_eq() {
  local expected=$1 actual=$2 label=${3:-value}
  if [[ "${actual}" != "${expected}" ]]; then
    bg_die "Unexpected ${label}: expected '${expected}', got '${actual}'."
    return 1
  fi
}

bg_assert_contains() {
  local haystack=$1 needle=$2 label=${3:-value}
  if [[ "${haystack}" != *"${needle}"* ]]; then
    bg_die "${label} did not contain '${needle}'."
    return 1
  fi
}

bg_container_name() {
  local scenario=$1 suffix=${2:-} raw safe
  if ! bg_require_cmd tr sed; then
    return 1
  fi
  raw="${scenario}${suffix:+-${suffix}}"
  safe=$(printf '%s' "${raw}" |
    tr '[:upper:]' '[:lower:]' |
    sed -E 's/[^a-z0-9_.-]+/-/g; s/^-+//; s/-+$//')
  if [[ -z "${safe}" ]]; then
    bg_die "Could not derive a safe Docker name from '${raw}'."
    return 1
  fi
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
  if ! bg_require_cmd curl; then
    return 1
  fi
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep "${delay}"
  done
  bg_die "Timed out waiting for ${url}."
  return 1
}

bg_build_image() {
  if ! bg_require_cmd docker git; then
    return 1
  fi
  if ! bg_assert_expected_head; then
    return 1
  fi
  local image short_head
  if ! short_head=$(bg_repo_short_head); then
    return 1
  fi
  image=${BOOKGUARD_ACCEPTANCE_IMAGE:-bookguard:acceptance-${short_head}}
  bg_header "BUILD ${image}"
  docker build --tag "${image}" "${BG_ACCEPTANCE_REPO_ROOT}"
  export BG_ACCEPTANCE_IMAGE_RESOLVED="${image}"
  printf '%s\n' "${image}"
}
