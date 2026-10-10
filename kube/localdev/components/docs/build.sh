#!/bin/bash
# Build hook for `mise run build docs` (called with IMAGE and LOCALDEV_ROOT set; must build $IMAGE).
# The markdown lives outside the component, in $LOCALDEV_ROOT/docs, so stage a build context:
# this component's docker/ files + a copy of docs/. The multi-stage Dockerfile renders it with MkDocs.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
cp -r "$here/docker/." "$ctx"
cp -r "$LOCALDEV_ROOT/docs" "$ctx/docs"
docker build -t "$IMAGE" "$ctx"
