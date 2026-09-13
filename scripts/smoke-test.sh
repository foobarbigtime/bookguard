#!/usr/bin/env bash
set -euo pipefail

REPOSITORY_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "${REPOSITORY_ROOT}"

SMOKE_IMAGE=${BOOKGUARD_SMOKE_IMAGE:-bookguard-smoke:local}

echo "=== BUILD ISOLATED SMOKE IMAGE ==="
docker build --tag "${SMOKE_IMAGE}" .

compose_json() {
  BOOKGUARD_AUTH_PASSWORD=isolated-smoke-test-only \
  BOOKGUARD_ALLOW_ACTIONS=false \
  BOOKGUARD_AUTOMATIC_REACQUISITION=false \
  BOOKGUARD_EBOOK_ACTIONS_ENABLED=false \
  BOOKGUARD_ADMISSION_ENABLED=false \
    docker compose --env-file /dev/null "$@" config --format json
}

check_compose_profile() {
  local profile=$1
  shift
  compose_json "$@" |
    docker run \
      --rm \
      --interactive \
      --network none \
      --read-only \
      "${SMOKE_IMAGE}" \
      python -m tools.smoke_test compose "${profile}"
}

echo
echo "=== BASE COMPOSE SAFETY ==="
check_compose_profile base --file compose.yaml

echo
echo "=== ACTION OVERLAY SAFETY ==="
check_compose_profile \
  actions \
  --file compose.yaml \
  --file compose.actions.yaml

echo
echo "=== ADMISSION OVERLAY SAFETY ==="
check_compose_profile \
  admission \
  --file compose.yaml \
  --file compose.admission.yaml

echo
echo "=== COMBINED ACTION + ADMISSION SAFETY ==="
check_compose_profile \
  full \
  --file compose.yaml \
  --file compose.actions.yaml \
  --file compose.admission.yaml

echo
echo "=== ISOLATED WORKFLOW ==="
docker run \
  --rm \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m \
  "${SMOKE_IMAGE}" \
  python -m tools.smoke_test workflow

echo
echo "PASS: BookGuard smoke tests completed without mounting live data or using a network."
