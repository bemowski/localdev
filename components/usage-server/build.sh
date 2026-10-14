#!/bin/bash
# Build the server image from docker/. localdev's `mise run build usage-server` calls this with IMAGE set;
# standalone, IMAGE defaults to usage-server:dev. PLATFORM (e.g. linux/amd64, set by `mise run release`)
# builds for another architecture. The API spec lives next to docker/ (with examples/ and api/), so the
# build context is staged: docker/ + openapi.yaml.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-usage-server:dev}"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
cp -r "$here/docker/." "$ctx"
cp "$here/openapi.yaml" "$ctx"
find "$ctx" -name __pycache__ -prune -exec rm -rf {} +
docker build ${PLATFORM:+--platform "$PLATFORM"} -t "$IMAGE" "$ctx"
