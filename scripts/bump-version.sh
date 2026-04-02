#!/usr/bin/env bash
# Usage: ./scripts/bump-version.sh [major|minor|patch]
set -euo pipefail

BUMP="${1:-patch}"
CONFIG="cinexis_remote/config.yaml"

CURRENT=$(grep '^version:' "${CONFIG}" | sed 's/version: *"\?\([^"]*\)"\?/\1/')
MAJOR=$(echo "${CURRENT}" | cut -d. -f1)
MINOR=$(echo "${CURRENT}" | cut -d. -f2)
PATCH=$(echo "${CURRENT}" | cut -d. -f3)

case "${BUMP}" in
  major) MAJOR=$((MAJOR+1)); MINOR=0; PATCH=0 ;;
  minor) MINOR=$((MINOR+1)); PATCH=0 ;;
  patch) PATCH=$((PATCH+1)) ;;
  *)     echo "Usage: $0 [major|minor|patch]"; exit 1 ;;
esac

NEW="${MAJOR}.${MINOR}.${PATCH}"
sed -i "s/^version: .*/version: \"${NEW}\"/" "${CONFIG}"

echo "Bumped: ${CURRENT} → ${NEW}"
echo ""
echo "Next steps:"
echo "  git add -A"
echo "  git commit -m \"Release v${NEW}: <description>\""
echo "  git push"
echo ""
echo "GitHub Actions will automatically:"
echo "  Build amd64 + aarch64 + armv7 images"
echo "  Push to ghcr.io/cinexisin/cinexis-remote-access/{arch}:${NEW}"
echo "  Create GitHub Release v${NEW}"
