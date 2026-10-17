#!/usr/bin/env bash
set -euo pipefail

container="${BOOKGUARD_CONTAINER:-bookguard}"
default_backup_root="/mnt/cache/appdata/bookguard-backups"

usage() {
  cat <<'EOF'
Usage:
  scripts/bookguard-backup.sh create [backup-root]
  scripts/bookguard-backup.sh validate <backup-bundle>
  scripts/bookguard-backup.sh restore-validate <backup-bundle>

The helper reuses the exact image and runtime UID:GID of the existing BookGuard
container. Creation mounts /config read-only and only the backup destination
writable. Validation mounts the selected backup parent read-only. Restore validation
copies the validated database only into a disposable container tmpfs and runs
the current BookGuard startup database initialization against that copy. These
commands run with no network, a read-only image root, no added capabilities,
and no-new-privileges.
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
runtime_uid="${runtime_user%%:*}"
runtime_gid="${runtime_user##*:}"
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

    resolved_backup_root="$(readlink -f "$backup_root")"
    if ! docker run "${common_args[@]}" -v "$resolved_backup_root:/backups" "$image_id" python -c 'import os,sys; sys.exit(0 if os.access("/backups", os.W_OK | os.X_OK) else 1)'; then
      echo "Backup root is not writable by BookGuard runtime user $runtime_user: $resolved_backup_root" >&2
      echo "Adjust only that backup directory's ownership/permissions, then retry." >&2
      exit 1
    fi

    docker run "${common_args[@]}" -v "$config_source:/config:ro" -v "$resolved_backup_root:/backups" "$image_id" python -m tools.bookguard_backup create --config-dir /config --backup-root /backups
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

    docker run "${common_args[@]}" -v "$parent:/backup-parent:ro" "$image_id" python -m tools.bookguard_backup validate "/backup-parent/$name"
    ;;

  restore-validate)
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

    # SQLite may need extra scratch space during startup migrations. A fixed
    # 512 MiB mount rejects healthy backups as soon as the database grows.
    # The tmpfs size is a maximum, not an up-front RAM allocation.
    database_file="$bundle/bookguard.db"
    if [[ ! -f "$database_file" || -L "$database_file" ]]; then
      echo "Restore validation requires a regular bookguard.db in $bundle." >&2
      exit 1
    fi
    database_bytes="$(stat -c '%s' -- "$database_file")"
    if [[ ! "$database_bytes" =~ ^[0-9]+$ || "$database_bytes" -eq 0 ]]; then
      echo "Unable to determine a positive database size for restore validation." >&2
      exit 1
    fi
    database_mib=$(( (database_bytes + 1048575) / 1048576 ))
    restore_tmpfs_mib=$(( database_mib * 2 + 256 ))
    if (( restore_tmpfs_mib < 512 )); then
      restore_tmpfs_mib=512
    fi
    echo "Disposable restore tmpfs limit: ${restore_tmpfs_mib} MiB for ${database_bytes} database bytes."

    docker run "${common_args[@]}" \
      --tmpfs "/restore:rw,nosuid,nodev,noexec,size=${restore_tmpfs_mib}m,uid=$runtime_uid,gid=$runtime_gid,mode=0700" \
      -v "$parent:/backup-parent:ro" \
      "$image_id" \
      python -m tools.bookguard_backup restore-validate \
        "/backup-parent/$name" \
        --restore-dir /restore
    ;;

  *)
    usage >&2
    exit 2
    ;;
esac
