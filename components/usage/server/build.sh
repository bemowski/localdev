#!/bin/bash
# Build the server image from docker/. localdev's `mise run build usage` calls this with IMAGE set;
# standalone, IMAGE defaults to usage-server:dev. The API spec lives one level up (shared with the
# client), so the build context is staged: docker/ + ../openapi.yaml.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${IMAGE:-usage-server:dev}"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
cp -r "$here/docker/." "$ctx"
cp "$here/../openapi.yaml" "$ctx"
find "$ctx" -name __pycache__ -prune -exec rm -rf {} +
docker build -t "$IMAGE" "$ctx"
