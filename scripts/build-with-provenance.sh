#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! git diff --quiet --ignore-submodules -- || ! git diff --cached --quiet --ignore-submodules --; then
  echo "Refusing provenance build: tracked files have uncommitted changes." >&2
  exit 1
fi

revision="$(git rev-parse HEAD)"
version="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' app/__init__.py | head -n 1)"

if [[ -z "$version" ]]; then
  echo "Unable to determine BookGuard version from app/__init__.py." >&2
  exit 1
fi

echo "Building BookGuard with source provenance"
echo "  version:  $version"
echo "  revision: $revision"

docker compose build \
  --build-arg "BOOKGUARD_VERSION=$version" \
  --build-arg "BOOKGUARD_VCS_REF=$revision" \
  bookguard

image_ref="$(docker compose config --images | head -n 1)"
if [[ -z "$image_ref" ]]; then
  echo "Unable to determine the Compose image name for BookGuard." >&2
  exit 1
fi

actual_version="$(docker image inspect "$image_ref" --format '{{ index .Config.Labels "org.opencontainers.image.version" }}')"
actual_revision="$(docker image inspect "$image_ref" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
actual_source="$(docker image inspect "$image_ref" --format '{{ index .Config.Labels "org.opencontainers.image.source" }}')"

if [[ "$actual_version" != "$version" ]]; then
  echo "Provenance verification failed: image version '$actual_version' != '$version'." >&2
  exit 1
fi

if [[ "$actual_revision" != "$revision" ]]; then
  echo "Provenance verification failed: image revision '$actual_revision' != '$revision'." >&2
  exit 1
fi

if [[ "$actual_source" != "https://github.com/foobarbigtime/bookguard" ]]; then
  echo "Provenance verification failed: unexpected image source '$actual_source'." >&2
  exit 1
fi

echo
echo "Provenance verified:"
echo "  image:    $image_ref"
echo "  version:  $actual_version"
echo "  revision: $actual_revision"
echo "  source:   $actual_source"
