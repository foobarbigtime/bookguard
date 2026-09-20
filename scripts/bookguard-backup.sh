#!/usr/bin/env bash
set -euo pipefail

container="${BOOKGUARD_CONTAINER:-bookguard}"
default_backup_root="/mnt/cache/appdata/bookguard-backups"

usage() {
  cat <<'EOF'
Usage:
  scripts/bookguard-backup.sh create [backup-root]
  scripts/bookguard-backup.sh validate <backup-bundle>

The helper reuses the exact image and runtime UID:GID of the existing BookGuard
container. Creation mounts /config read-only and only the backup destination
writable. Validation mounts the selected backup parent read-only. Both commands
run with no network, a read-only image root, no added capabilities, and
no-new-privileges.
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

command_name="$1"
shift

if ! docker inspect "$container" >/dev/null 2>&1; then
  echo "BookGuard container '$container' was not found." >&2
  exit 1
fi

image_id="${BOOKGUARD_BACKUP_IMAGE:-$(docker inspect "$container" --format '{{.Image}}')}"
runtime_user="$(docker inspect "$container" --format '{{.Config.User}}')"
config_source="$(
  docker inspect "$container"     --format '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}'
)"

if [[ -z "$image_id" ]]; then
  echo "Unable to determine the BookGuard container image." >&2
  exit 1
fi
if ! docker image inspect "$image_id" >/dev/null 2>&1; then
  echo "BookGuard backup image was not found locally: $image_id" >&2
  exit 1
fi
if [[ -z "$runtime_user" || "$runtime_user" == "0" || "$runtime_user" == "root" ]]; then
  echo "Refusing backup helper: BookGuard runtime user is not an explicit non-root UID:GID." >&2
  exit 1
fi
if [[ -z "$config_source" || ! -d "$config_source" ]]; then
  echo "Unable to determine BookGuard's /config host directory." >&2
  exit 1
fi

common_args=(
  --rm
  --network none
  --user "$runtime_user"
  --read-only
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m
  --security-opt no-new-privileges:true
  --cap-drop ALL
)

case "$command_name" in
  create)
    if [[ $# -gt 1 ]]; then
      usage >&2
      exit 2
    fi
    backup_root="${1:-$default_backup_root}"
    if [[ ! -e "$backup_root" ]]; then
      mkdir -p "$backup_root"
      chown "$runtime_user" "$backup_root"
      chmod 0750 "$backup_root"
    fi
    if [[ ! -d "$backup_root" ]]; then
      echo "Backup root is not a directory: $backup_root" >&2
      exit 1
    fi
    if [[ "$(readlink -f "$backup_root")" == "$(readlink -f "$config_source")" ]]; then
      echo "Backup root must be separate from BookGuard's config directory." >&2
      exit 1
    fi

    docker run "${common_args[@]}"       -v "$config_source:/config:ro"       -v "$(readlink -f "$backup_root"):/backups"       "$image_id"       python -m tools.bookguard_backup create         --config-dir /config         --backup-root /backups
    ;;

  validate)
    if [[ $# -ne 1 ]]; then
      usage >&2
      exit 2
    fi
    bundle="$(readlink -f "$1")"
    if [[ ! -d "$bundle" ]]; then
      echo "Backup bundle is not a directory: $1" >&2
      exit 1
    fi
    parent="$(dirname "$bundle")"
    name="$(basename "$bundle")"

    docker run "${common_args[@]}"       -v "$parent:/backup-parent:ro"       "$image_id"       python -m tools.bookguard_backup validate "/backup-parent/$name"
    ;;

  *)
    usage >&2
    exit 2
    ;;
esac
