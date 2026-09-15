#!/usr/bin/env bash
# Bump every version literal at once, so none can be left behind.
#
# There are exactly three, and they live in different files:
#   1. pyproject.toml            `version = "X.Y.Z"`   -> becomes EXPORTER_VERSION at runtime
#   2. .github/workflows/export.yml  `EXPORTER_TAG:`   -> the checkout ref AND the guard's
#                                                          expected version, both derived
#   3. docs/examples/connector-export.yml  five `uses: ...@exporter-vX.Y.Z` lines -> the
#                                          SOURCE OF TRUTH caller every contractor copies
#
# (3) was missed by hand once and the file shipped pinned to `exporter-v0.3.0`, a tag
# that does not exist: a contractor copying it gets a workflow GitHub cannot resolve,
# and every feed AND the drain stop. It is rewritten here now, and
# `tests/st_exporter/test_workflow.py` asserts it equals export.yml's EXPORTER_TAG.
#
# They are cross-checked at runtime by the "Verify the checked-out exporter"
# step, so a mismatch fails the customer's run loudly instead of silently
# exporting the wrong code. Bumping them by hand is how v0.2.1 shipped wrong
# and how v0.2.7 failed twice before it worked. Use this instead.
#
#   ./scripts/release.sh 0.2.8
#
# YOU PROBABLY DO NOT NEED TO RUN THIS BY HAND ANY MORE.
# `.github/workflows/release.yml` runs this script on every merge into the
# integration branch, then commits, tags the commit it just made, and pushes both
# atomically. Merging is now the whole release. This script stays hand-runnable
# (it is the ONE place that knows where the three literals live, and the workflow
# calls it rather than copying its seds), but the commit/tag dance below is the
# part that has been got wrong four times, and it is no longer yours to do:
#
#   * a version out of sequence, or a release of something the rules skip:
#     Actions -> Release -> Run workflow (`version:` / `bump:` / `force:`).
#   * a rehearsal that pushes nothing: the same, with `dry_run: true`.
#
# If you do run it by hand anyway, the ordering constraint is yours to satisfy:
# the tag must point at a commit whose EXPORTER_TAG already names that tag — the
# workflow published at tag X must itself check out X.
#
#   git commit -am "chore: release 0.2.8" && git push
#   # merge, then tag the MERGED commit:
#   gh api repos/BBTT-01/ST-exporter/git/refs -f ref=refs/tags/exporter-v0.2.8 -f sha=<sha>
set -euo pipefail

VERSION="${1:-}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "usage: $0 <X.Y.Z>   (got '${VERSION:-<nothing>}')" >&2
  exit 1
fi

cd "$(dirname "$0")/.."
PYPROJECT="pyproject.toml"
WORKFLOW=".github/workflows/export.yml"
CALLER="docs/examples/connector-export.yml"

current="$(sed -n 's/^version = "\(.*\)"/\1/p' "$PYPROJECT" | head -1)"
echo "  $current -> $VERSION"

# BSD and GNU sed disagree about -i; write via a temp file instead.
tmp="$(mktemp)"
sed "s/^version = \".*\"/version = \"$VERSION\"/" "$PYPROJECT" > "$tmp" && mv "$tmp" "$PYPROJECT"
tmp="$(mktemp)"
sed "s/^      EXPORTER_TAG: exporter-v.*/      EXPORTER_TAG: exporter-v$VERSION/" "$WORKFLOW" > "$tmp" && mv "$tmp" "$WORKFLOW"
tmp="$(mktemp)"
sed "s|\(export\.yml\)@exporter-v[0-9]*\.[0-9]*\.[0-9]*|\1@exporter-v$VERSION|" "$CALLER" > "$tmp" && mv "$tmp" "$CALLER"

got_py="$(sed -n 's/^version = "\(.*\)"/\1/p' "$PYPROJECT" | head -1)"
got_wf="$(sed -n 's/^      EXPORTER_TAG: exporter-v\(.*\)/\1/p' "$WORKFLOW" | head -1)"
# Every `uses:` line in the caller, deduplicated: "0.2.9" if they agree, and
# something with a newline in it (so the check below fails) if they do not.
got_caller="$(grep -o 'export\.yml@exporter-v[0-9.]*' "$CALLER" | sed 's/.*@exporter-v//' | sort -u | tr '\n' ' ' | sed 's/ $//')"
if [[ "$got_py" != "$VERSION" || "$got_wf" != "$VERSION" || "$got_caller" != "$VERSION" ]]; then
  echo "FAILED: pyproject='$got_py' workflow='$got_wf' caller='$got_caller' — expected all '$VERSION'" >&2
  exit 1
fi

echo "  pyproject.toml          version = \"$got_py\""
echo "  export.yml         EXPORTER_TAG: exporter-v$got_wf"
echo "  connector-export.yml      uses: ...@exporter-v$got_caller (x$(grep -c 'export\.yml@exporter-v' "$CALLER"))"
echo
echo "Next: add a CHANGELOG entry and merge. Merging into the integration branch cuts"
echo "      exporter-v$VERSION by itself (.github/workflows/release.yml); tag nothing by hand."
