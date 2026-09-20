#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

container="${BOOKGUARD_CONTAINER:-bookguard}"
clamav_container="${BOOKGUARD_CLAMAV_CONTAINER:-bookguard-clamav}"
backup_root="${BOOKGUARD_BACKUP_ROOT:-/mnt/cache/appdata/bookguard-backups}"
health_wait_seconds="${BOOKGUARD_UPGRADE_HEALTH_WAIT_SECONDS:-180}"
confirmation="DEPLOY_BOOKGUARD_UPGRADE"
compose_files=(-f compose.yaml -f compose.clamav.yaml)

candidate_image_ref=""
candidate_image_id=""
source_revision=""
source_version=""
backup_bundle=""
rollback_tag=""

usage() {
  cat <<'EOF'
Usage:
  scripts/safer-upgrade.sh preflight
  scripts/safer-upgrade.sh verify
  scripts/safer-upgrade.sh upgrade --confirm DEPLOY_BOOKGUARD_UPGRADE

preflight
  Verifies a clean, synchronized Git checkout, runs the isolated smoke suite,
  and builds/verifies the candidate image with source provenance. It does not
  touch the running production container.

verify
  Verifies the currently deployed BookGuard image provenance, health, runtime
  hardening, ClamAV isolation, and live ClamAV/EICAR acceptance.

upgrade
  Requires the main branch and the exact confirmation token. It reruns all
  preflight checks, creates and validates a BookGuard database backup using the
  candidate image, proves restore compatibility in disposable tmpfs, tags the
  currently deployed image for rollback, deploys only the normal base+ClamAV
  topology with --no-build, then runs all post-deployment verification.

The helper never automatically rolls back or restores database state after a
failed deployment. It preserves the rollback image tag and validated backup and
prints them for explicit operator recovery.
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Required command not found: $1" >&2
    exit 1
  }
}

source_state_check() {
  require_command git
  require_command docker

  if [[ -n "$(git status --porcelain=v1 --untracked-files=normal)" ]]; then
    echo "Refusing upgrade workflow: Git working tree is not clean." >&2
    git status --short >&2 || true
    exit 1
  fi

  local branch
  branch="$(git symbolic-ref --quiet --short HEAD || true)"
  if [[ -z "$branch" ]]; then
    echo "Refusing upgrade workflow: detached HEAD is not allowed." >&2
    exit 1
  fi

  if ! git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    echo "Refusing upgrade workflow: branch '$branch' has no upstream." >&2
    exit 1
  fi

  git fetch --quiet origin

  local ahead behind
  read -r ahead behind < <(git rev-list --left-right --count HEAD...'@{u}')
  if [[ "$ahead" != "0" || "$behind" != "0" ]]; then
    echo "Refusing upgrade workflow: local branch and upstream differ (ahead=$ahead behind=$behind)." >&2
    exit 1
  fi

  source_revision="$(git rev-parse HEAD)"
  source_version="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' app/__init__.py | head -n 1)"
  if [[ -z "$source_version" ]]; then
    echo "Unable to determine BookGuard version." >&2
    exit 1
  fi

  echo "Source state verified:"
  echo "  branch:   $branch"
  echo "  version:  $source_version"
  echo "  revision: $source_revision"
}

resolve_candidate_image() {
  candidate_image_ref="$(docker compose -f compose.yaml config --images | head -n 1)"
  if [[ -z "$candidate_image_ref" ]]; then
    echo "Unable to determine candidate BookGuard image reference." >&2
    exit 1
  fi
  candidate_image_id="$(docker image inspect "$candidate_image_ref" --format '{{.Id}}')"
  if [[ -z "$candidate_image_id" ]]; then
    echo "Unable to determine candidate BookGuard image ID." >&2
    exit 1
  fi
}

verify_image_provenance() {
  local image="$1"
  local expected_version="$2"
  local expected_revision="$3"

  local actual_version actual_revision actual_source
  actual_version="$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}')"
  actual_revision="$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
  actual_source="$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.source" }}')"

  [[ "$actual_version" == "$expected_version" ]] || {
    echo "Image provenance mismatch: version '$actual_version' != '$expected_version'." >&2
    return 1
  }
  [[ "$actual_revision" == "$expected_revision" ]] || {
    echo "Image provenance mismatch: revision '$actual_revision' != '$expected_revision'." >&2
    return 1
  }
  [[ "$actual_source" == "https://github.com/foobarbigtime/bookguard" ]] || {
    echo "Image provenance mismatch: unexpected source '$actual_source'." >&2
    return 1
  }

  echo "Image provenance verified:"
  echo "  image:    $image"
  echo "  version:  $actual_version"
  echo "  revision: $actual_revision"
  echo "  source:   $actual_source"
}

run_preflight() {
  echo "===== SOURCE STATE ====="
  source_state_check

  echo
  echo "===== ISOLATED SMOKE ====="
  ./scripts/smoke-test.sh

  echo
  echo "===== PROVENANCE BUILD ====="
  bash scripts/build-with-provenance.sh
  resolve_candidate_image
  verify_image_provenance "$candidate_image_ref" "$source_version" "$source_revision"

  echo
  echo "PASS: preflight completed without changing the running production container."
}

create_upgrade_backup() {
  [[ -n "$candidate_image_ref" ]] || {
    echo "Candidate image is not initialized." >&2
    return 1
  }

  echo "===== PRE-UPGRADE BACKUP ====="
  local backup_json bundle_container bundle_name
  backup_json="$(
    BOOKGUARD_BACKUP_IMAGE="$candidate_image_ref" \
      bash scripts/bookguard-backup.sh create "$backup_root"
  )"
  printf '%s\n' "$backup_json"

  bundle_container="$(
    printf '%s\n' "$backup_json" |
      docker run --rm -i --network none --read-only "$candidate_image_ref" \
        python -c 'import json,sys; print(json.load(sys.stdin)["bundle"])'
  )"
  bundle_name="${bundle_container##*/}"
  if [[ -z "$bundle_name" || "$bundle_name" == "$bundle_container" ]]; then
    echo "Unable to determine the created backup bundle." >&2
    return 1
  fi
  backup_bundle="$(readlink -f "$backup_root")/$bundle_name"

  echo
  echo "===== READ-ONLY BACKUP VALIDATION ====="
  BOOKGUARD_BACKUP_IMAGE="$candidate_image_ref" \
    bash scripts/bookguard-backup.sh validate "$backup_bundle"

  echo
  echo "===== DISPOSABLE RESTORE VALIDATION ====="
  BOOKGUARD_BACKUP_IMAGE="$candidate_image_ref" \
    bash scripts/bookguard-backup.sh restore-validate "$backup_bundle"

  echo
  echo "Validated pre-upgrade backup:"
  echo "  $backup_bundle"
}

wait_for_health() {
  local deadline=$((SECONDS + health_wait_seconds))
  local status=""

  while (( SECONDS < deadline )); do
    status="$(
      docker inspect "$container" \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        2>/dev/null || true
    )"
    if [[ "$status" == "healthy" ]]; then
      echo "BookGuard container is healthy."
      return 0
    fi
    if [[ "$status" == "exited" || "$status" == "dead" ]]; then
      echo "BookGuard container entered terminal state: $status" >&2
      return 1
    fi
    sleep 2
  done

  echo "Timed out waiting for BookGuard health; last state: ${status:-unknown}" >&2
  return 1
}

validate_running_topology() {
  local validator_image="$1"

  echo "===== BOOKGUARD RUNTIME HARDENING ====="
  docker inspect "$container" |
    docker run --rm -i --network none --read-only "$validator_image" \
      python -m tools.upgrade_safety runtime

  echo
  echo "===== CLAMAV RUNTIME ISOLATION ====="
  docker inspect "$clamav_container" |
    docker run --rm -i --network none --read-only "$validator_image" \
      python -m tools.upgrade_safety clamav-runtime
}

validate_health_endpoint() {
  local validator_image="$1"
  local expected_version="$2"
  local health_json reported_version

  health_json="$(docker exec "$container" curl -fsS http://127.0.0.1:8788/health)"
  printf '%s\n' "$health_json"

  reported_version="$(
    printf '%s\n' "$health_json" |
      docker run --rm -i --network none --read-only "$validator_image" \
        python -c 'import json,sys; print(json.load(sys.stdin).get("version",""))'
  )"
  if [[ "$reported_version" != "$expected_version" ]]; then
    echo "Health endpoint version '$reported_version' != expected '$expected_version'." >&2
    return 1
  fi
}

run_clamav_acceptance() {
  echo "===== LIVE CLAMAV ACCEPTANCE ====="
  docker compose "${compose_files[@]}" \
    exec -T "$container" python -m tools.clamav_acceptance
}

verify_deployment() {
  local expected_image_id="$1"
  local expected_version="$2"
  local expected_revision="$3"
  local validator_image="$4"

  wait_for_health

  local deployed_image_id
  deployed_image_id="$(docker inspect "$container" --format '{{.Image}}')"
  if [[ -n "$expected_image_id" && "$deployed_image_id" != "$expected_image_id" ]]; then
    echo "Deployed image '$deployed_image_id' != expected candidate '$expected_image_id'." >&2
    return 1
  fi

  verify_image_provenance "$deployed_image_id" "$expected_version" "$expected_revision"

  echo
  echo "===== HEALTH ENDPOINT ====="
  validate_health_endpoint "$validator_image" "$expected_version"

  echo
  validate_running_topology "$validator_image"

  echo
  run_clamav_acceptance

  echo
  echo "PASS: deployed BookGuard passed provenance, health, hardening, and ClamAV acceptance."
}

verify_current_deployment() {
  source_state_check
  resolve_candidate_image

  local deployed_image_id deployed_version deployed_revision
  deployed_image_id="$(docker inspect "$container" --format '{{.Image}}')"
  deployed_version="$(docker image inspect "$deployed_image_id" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}')"
  deployed_revision="$(docker image inspect "$deployed_image_id" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"

  if [[ -z "$deployed_version" || -z "$deployed_revision" || "$deployed_revision" == "unknown" ]]; then
    echo "Deployed BookGuard image does not have complete provenance." >&2
    exit 1
  fi

  verify_deployment "$deployed_image_id" "$deployed_version" "$deployed_revision" "$candidate_image_ref"
}

upgrade_failure_message() {
  local rc=$?
  echo >&2
  echo "UPGRADE VERIFICATION FAILED. No automatic rollback was attempted." >&2
  if [[ -n "$rollback_tag" ]]; then
    echo "Rollback image tag preserved: $rollback_tag" >&2
  fi
  if [[ -n "$backup_bundle" ]]; then
    echo "Validated database backup preserved: $backup_bundle" >&2
  fi
  echo "Inspect the failure before taking an explicit recovery action." >&2
  exit "$rc"
}

run_upgrade() {
  local branch
  branch="$(git symbolic-ref --quiet --short HEAD || true)"
  if [[ "$branch" != "main" ]]; then
    echo "Refusing production upgrade: current branch is '$branch', not 'main'." >&2
    exit 1
  fi

  run_preflight

  if ! docker inspect "$container" >/dev/null 2>&1; then
    echo "Refusing upgrade: running BookGuard container '$container' was not found." >&2
    exit 1
  fi

  echo
  create_upgrade_backup

  echo
  echo "===== FINAL SOURCE RECHECK ====="
  local built_revision="$source_revision"
  source_state_check
  if [[ "$source_revision" != "$built_revision" ]]; then
    echo "Refusing deployment: source revision changed after the candidate build." >&2
    exit 1
  fi

  local current_image_id timestamp
  current_image_id="$(docker inspect "$container" --format '{{.Image}}')"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  rollback_tag="${BOOKGUARD_ROLLBACK_TAG:-bookguard-bookguard:rollback-pre-upgrade-$timestamp}"
  if docker image inspect "$rollback_tag" >/dev/null 2>&1; then
    echo "Refusing to overwrite existing rollback tag: $rollback_tag" >&2
    exit 1
  fi

  echo
  echo "===== ROLLBACK IMAGE TAG ====="
  docker image tag "$current_image_id" "$rollback_tag"
  echo "Rollback image preserved as: $rollback_tag"

  trap upgrade_failure_message ERR

  echo
  echo "===== DEPLOY CANDIDATE ====="
  docker compose "${compose_files[@]}" up -d --no-build

  echo
  echo "===== POST-DEPLOY ACCEPTANCE ====="
  verify_deployment "$candidate_image_id" "$source_version" "$source_revision" "$candidate_image_ref"

  trap - ERR

  echo
  echo "Upgrade accepted."
  echo "  deployed image:  $candidate_image_ref"
  echo "  revision:        $source_revision"
  echo "  rollback image:  $rollback_tag"
  echo "  database backup: $backup_bundle"
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

command_name="$1"
shift

case "$command_name" in
  preflight)
    if [[ $# -ne 0 ]]; then
      usage >&2
      exit 2
    fi
    run_preflight
    ;;
  verify)
    if [[ $# -ne 0 ]]; then
      usage >&2
      exit 2
    fi
    verify_current_deployment
    ;;
  upgrade)
    if [[ $# -ne 2 || "$1" != "--confirm" || "$2" != "$confirmation" ]]; then
      echo "Production deployment requires: --confirm $confirmation" >&2
      exit 2
    fi
    run_upgrade
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
