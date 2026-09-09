#!/usr/bin/env bash
# Bump every version literal at once, so none can be left behind.
#
# There are exactly two, and they live in different files:
#   1. pyproject.toml            `version = "X.Y.Z"`   -> becomes EXPORTER_VERSION at runtime
#   2. .github/workflows/export.yml  `EXPORTER_TAG:`   -> the checkout ref AND the guard's
#                                                          expected version, both derived
#
# They are cross-checked at runtime by the "Verify the checked-out exporter"
# step, so a mismatch fails the customer's run loudly instead of silently
# exporting the wrong code. Bumping them by hand is how v0.2.1 shipped wrong
# and how v0.2.7 failed twice before it worked. Use this instead.
#
#   ./scripts/release.sh 0.2.8
#   git commit -am "chore: release 0.2.8" && git push
#   # merge, then tag the MERGED commit:
#   gh api repos/BBTT-01/ST-exporter/git/refs -f ref=refs/tags/exporter-v0.2.8 -f sha=<sha>
#
# The tag must point at a commit whose EXPORTER_TAG already names that tag —
# the workflow published at tag X must itself check out X.
set -euo pipefail

VERSION="${1:-}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: $0 <X.Y.Z>   (got '${VERSION:-<nothing>}')" >&2
  exit 1
fi

cd "$(dirname "$0")/.."
PYPROJECT="pyproject.toml"
WORKFLOW=".github/workflows/export.yml"

current="$(sed -n 's/^version = "\(.*\)"/\1/p' "$PYPROJECT" | head -1)"
echo "  $current -> $VERSION"

# BSD and GNU sed disagree about -i; write via a temp file instead.
tmp="$(mktemp)"
sed "s/^version = \".*\"/version = \"$VERSION\"/" "$PYPROJECT" > "$tmp" && mv "$tmp" "$PYPROJECT"
tmp="$(mktemp)"
sed "s/^      EXPORTER_TAG: exporter-v.*/      EXPORTER_TAG: exporter-v$VERSION/" "$WORKFLOW" > "$tmp" && mv "$tmp" "$WORKFLOW"

got_py="$(sed -n 's/^version = "\(.*\)"/\1/p' "$PYPROJECT" | head -1)"
got_wf="$(sed -n 's/^      EXPORTER_TAG: exporter-v\(.*\)/\1/p' "$WORKFLOW" | head -1)"
if [[ "$got_py" != "$VERSION" || "$got_wf" != "$VERSION" ]]; then
  echo "FAILED: pyproject='$got_py' workflow='$got_wf' — expected both '$VERSION'" >&2
  exit 1
fi

echo "  pyproject.toml          version = \"$got_py\""
echo "  export.yml         EXPORTER_TAG: exporter-v$got_wf"
echo
echo "Next: add a CHANGELOG entry, commit, merge, then tag the merged commit exporter-v$VERSION"
