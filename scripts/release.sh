#!/bin/bash
set -e
set -o pipefail

# CompreDef Release Script — ONE command, full pipeline:
#
#   ./scripts/release.sh [vX.Y.Z]
#
#   tests → package .ankiaddon → commit → push → tag → GitHub Release
#     → CI uploads to AnkiWeb (see .github/workflows/upload-to-ankiweb.yml)
#
# After CI finishes, the user's test loop is exactly:
#   1. restart Anki            (addon update check fires)
#   2. Anki shows "Update All" / auto-updates CompreDef in ~1s
#   3. restart Anki            (new code active)
#   4. test the change
# No manual .ankiaddon install is needed once the addon is installed
# from AnkiWeb (id 1619602654) — updates flow through automatically.

# Name the failing step/command instead of dying with a generic message.
trap 'echo ""; echo "!!! release.sh FAILED at step in progress: $BASH_COMMAND (line $LINENO)"' ERR

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Version determination (before build so manifest matches the release)
VERSION_ARG="$1"
if [ -n "$VERSION_ARG" ]; then
    VERSION="$VERSION_ARG"
elif [ -f VERSION ]; then
    VERSION="$(tr -d '[:space:]' < VERSION)"
else
    VERSION="v1.0.0"
fi

# Ensure version has 'v' prefix for git tag, and clean version for manifest
if [[ "$VERSION" =~ ^v[0-9] ]]; then
    TAG="$VERSION"
    NUMERIC_VER="${VERSION#v}"
else
    TAG="v$VERSION"
    NUMERIC_VER="$VERSION"
fi

echo "$TAG" > VERSION
echo "Target Release: $TAG (Add-on version: $NUMERIC_VER)"

echo "=== [1/6] Updating manifest.json ==="
python3 -c "
import json
manifest_path = 'manifest.json'
try:
    with open(manifest_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    data = {}
data['package'] = '1619602654'  # AnkiWeb ID: enables 'View Add-on Page' + correct install folder
data['name'] = 'CompreDef'
data['human_version'] = '$NUMERIC_VER'
with open(manifest_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)
    f.write('\n')
"

# Tests + packaging + verification, all in the shared build script
echo "=== [2/6] Building .ankiaddon (tests + package + verify) ==="
./scripts/build.sh
ANKIADDON_OUT="dist/CompreDef.ankiaddon"

echo "=== [3/6] Committing changes & pushing to GitHub ==="
# Stage EVERYTHING (source, tests, docs, meta). A hardcoded subset went
# stale after the module refactor — new files (provider.py, renderer.py,
# ...) were never staged, the commit found nothing, and the pipeline died.
git add -A
if git diff-index --quiet HEAD --; then
    echo "Working tree clean, no new commit needed."
else
    git commit -m "Prepare release $TAG"
fi

git push origin master

# Create or replace tag
if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "Tag $TAG exists locally; replacing..."
    git tag -d "$TAG"
    git push origin :refs/tags/"$TAG" 2>/dev/null || true
fi

git tag -a "$TAG" -m "Release $TAG"
git push origin "$TAG"

echo "=== [4/6] Creating GitHub Release with .ankiaddon attachment ==="
if gh release view "$TAG" >/dev/null 2>&1; then
    echo "Updating existing GitHub release $TAG..."
    gh release upload "$TAG" "$ANKIADDON_OUT" --clobber
else
    gh release create "$TAG" "$ANKIADDON_OUT" \
        --title "CompreDef $TAG" \
        --notes "## CompreDef $TAG

### Installation
1. Download **\`CompreDef.ankiaddon\`** below.
2. In Anki, go to **Tools → Add-ons → Install from file...**
3. Select \`CompreDef.ankiaddon\` and restart Anki.
4. Open **Tools → Add-ons → CompreDef → Config** to configure your note fields and dictionary ladder.

Already installed from AnkiWeb? Just restart Anki twice (update check → install → restart) and the new version is active."
fi

echo "=== [5/6] Waiting for AnkiWeb upload workflow ==="
# The release 'published' event triggers .github/workflows/upload-to-ankiweb.yml,
# which pushes the .ankiaddon to AnkiWeb. Surface its result here so a failed
# upload is never silently missed (it failed silently once already).
sleep 5
RUN_ID=$(gh run list --workflow=upload-to-ankiweb.yml --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null || echo "")
if [ -n "$RUN_ID" ]; then
    if gh run watch "$RUN_ID" --exit-status >/dev/null 2>&1; then
        echo "AnkiWeb upload: SUCCESS (run $RUN_ID)"
        echo "AnkiWeb page: https://ankiweb.net/shared/info/1619602654"
    else
        echo "WARNING: AnkiWeb upload workflow FAILED (run $RUN_ID)."
        echo "Check: gh run view $RUN_ID --log-failed"
        echo "Common cause: ANKI_WEB_USERNAME / ANKI_WEB_PASSWORD secrets missing:"
        echo "  gh secret set ANKI_WEB_USERNAME; gh secret set ANKI_WEB_PASSWORD"
        gh run view "$RUN_ID" --log-failed 2>/dev/null | tail -20 || true
        exit 1
    fi
else
    echo "NOTE: could not query workflow runs (gh run list failed)."
    echo "Verify manually: gh run list --workflow=upload-to-ankiweb.yml"
fi

echo "======================================================================"
echo "SUCCESS: CompreDef $TAG released — GitHub + AnkiWeb"
echo "User test loop: restart Anki → update → restart Anki → test"
echo "Release URL: $(gh release view "$TAG" --json url -q .url)"
echo "======================================================================"

echo "=== [6/6] Auto local install (final step, for immediate testing) ==="
if ./scripts/install_local.sh 2>&1; then
    echo "Auto local install: OK — restart Anki once to see $TAG locally (no AnkiWeb wait)"
else
    echo "Auto local install: FAILED (non-fatal — AnkiWeb update still works, install manually)"
fi
