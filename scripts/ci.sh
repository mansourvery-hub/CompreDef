#!/bin/bash
set -e

# CompreDef CI Script — test, commit, push, release (full pipeline).
#
# Every code change should end up testable by the user with nothing more
# than: restart Anki → update → restart Anki → test. This script makes
# that happen by chaining the regression suite, a git commit/push and a
# full release (GitHub Release + AnkiWeb upload via CI).
#
# Usage:
#   ./scripts/ci.sh            # test + commit + push + release (auto version bump)
#   ./scripts/ci.sh --no-release   # test + commit + push only
#
# Version bump strategy: patch +1 on every release (v1.0.2 -> v1.0.3 ...).
# Pass an explicit version to ./scripts/release.sh to override.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== [1/5] Running regression tests ==="
python3 tests/test_regression.py

echo "=== [2/5] Committing and pushing changes ==="
git add .
if git diff-index --quiet HEAD --; then
    echo "No changes to commit"
else
    git commit -m "Auto-commit: regression tests passed ($(date +%Y-%m-%d\ %H:%M))"
fi
git push origin master

if [ "$1" = "--no-release" ]; then
    echo "=== Skipped release (--no-release) ==="
    exit 0
fi

echo "=== [3/5] Determining next version ==="
# Auto-bump the patch component: v1.0.2 -> v1.0.3
CURRENT="$(tr -d '[:space:]' < VERSION 2>/dev/null || echo v1.0.0)"
CURRENT_NUM="${CURRENT#v}"
MAJOR="$(echo "$CURRENT_NUM" | cut -d. -f1)"
MINOR="$(echo "$CURRENT_NUM" | cut -d. -f2)"
PATCH="$(echo "$CURRENT_NUM" | cut -d. -f3)"
NEXT_NUM="${MAJOR}.${MINOR}.$((PATCH + 1))"
echo "Version: $CURRENT -> v$NEXT_NUM"

echo "=== [4/5] Releasing (GitHub Release + AnkiWeb upload) ==="
./scripts/release.sh "v$NEXT_NUM"

echo "=== [5/5] Auto local install (final step, for immediate testing) ==="
if ./scripts/install_local.sh 2>&1; then
    echo "Auto local install: OK — restart Anki once"
else
    echo "Auto local install: FAILED (non-fatal — AnkiWeb update still works)"
fi
