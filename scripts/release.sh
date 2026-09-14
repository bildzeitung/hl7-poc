#!/bin/bash -e
#
# Kick off a harness release: gate the tree, tag, and push the tag.
#
# Run from the repo root: scripts/release.sh X.Y.Z [notes-file]
#
# Guards that we're on main with a clean working tree, that local main is
# up to date with origin/main, that X.Y.Z is well-formed SemVer strictly
# greater than the latest existing tag, that vX.Y.Z doesn't already exist
# (locally or on origin), and that the full test suite (nox -s tests) is
# green; then creates an annotated tag on HEAD and pushes it to origin.
# An optional notes-file becomes the tag BODY — the release notes that
# .github/workflows/release.yml publishes; the subject stays "harness vX.Y.Z"
# either way. Stops there — the workflow owns the actual
# build+publish, triggered by the tag push. See docs/release.md for the full
# flow.

# Resolve the notes file against the caller's cwd, before the cd below.
NOTES_FILE="${2:-}"
if [ -n "$NOTES_FILE" ]; then
  if [ ! -s "$NOTES_FILE" ]; then
    echo "release.sh: notes file '$NOTES_FILE' not found or empty" >&2
    exit 1
  fi
  NOTES_FILE="$(realpath "$NOTES_FILE")"
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

VERSION="${1:?usage: scripts/release.sh X.Y.Z [notes-file]}"
if ! echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "release.sh: version must be X.Y.Z (semver, no leading 'v'), got '$VERSION'" >&2
  exit 1
fi
TAG="v$VERSION"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "main" ]; then
  echo "release.sh: must be run on main (currently on '$BRANCH')" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "release.sh: working tree is dirty — commit or stash first" >&2
  exit 1
fi

git fetch origin --tags
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "release.sh: local main is not up to date with origin/main — pull/push before releasing" >&2
  exit 1
fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "release.sh: tag $TAG already exists locally" >&2
  exit 1
fi

if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
  echo "release.sh: tag $TAG already exists on origin" >&2
  exit 1
fi

# SemVer monotonicity gate, via the shared scripts/release-latest-tag.sh
# -- this used to be an inline tag-selection loop + version_gt
# comparator duplicated (and free to drift) between this file and
# .claude/skills/release/SKILL.md; see that script's header comment for the
# full extraction rationale.
#
# NOTE: this is deliberately `if scripts/...; then:; else ... fi`, never
# `if ! scripts/...; then`. Negating the condition with `!` would still
# branch correctly, but `!` collapses EVERY non-zero status to 0, so `$?`
# inside the branch reads 0 whether the script really exited 1 or 2 --
# verified by hand -- making it impossible to tell "does not exceed" (exit 1)
# apart from "machine fault" (exit 2) once inside the branch. The empty
# `then :` arm is what lets the real status survive into `else`.
if scripts/release-latest-tag.sh --gt "$VERSION"; then
  :
else
  status=$?
  if [ "$status" -eq 2 ]; then
    echo "release.sh: scripts/release-latest-tag.sh could not run -- see its stderr above" >&2
    exit 1
  fi
  LATEST="$(scripts/release-latest-tag.sh)"
  echo "release.sh: $VERSION does not exceed latest existing tag $LATEST" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "release.sh: 'uv' not found on PATH — install uv first" >&2
  exit 1
fi
uv run --frozen nox -s tests

if [ -n "$NOTES_FILE" ]; then
  # --cleanup=whitespace: the default 'strip' deletes '#'-prefixed lines,
  # which would eat the notes' markdown headings.
  { echo "harness $TAG"; echo; cat "$NOTES_FILE"; } | git tag -a "$TAG" --cleanup=whitespace -F -
else
  git tag -a "$TAG" -m "harness $TAG"
fi
git push origin "$TAG"

echo "release.sh: pushed $TAG — CI will build + publish the release"
