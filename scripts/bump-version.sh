#!/usr/bin/env bash
# Usage: ./scripts/bump-version.sh [major|minor|patch]
# Example: ./scripts/bump-version.sh patch  → 1.0.1 → 1.0.2

set -euo pipefail

BUMP="${1:-patch}"
CONFIG="cinexis_remote/config.yaml"

# Read current version
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

# Update config.yaml
sed -i "s/^version: .*/version: \"${NEW}\"/" "${CONFIG}"

echo "✅ Bumped: ${CURRENT} → ${NEW}"
echo ""
echo "Next steps:"
echo "  1. Update CHANGELOG.md with changes for v${NEW}"
echo "  2. git add -A && git commit -m \"Release v${NEW}\""
echo "  3. git tag v${NEW} && git push && git push --tags"
