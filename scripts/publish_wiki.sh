#!/bin/bash
# Mirrors WIKI.md (the versioned source of truth) to the GitHub wiki.
# First-time setup (one click, GitHub creates *.wiki.git only then):
#   open https://github.com/mansourvery-hub/CompreDef/wiki and create any page.
# Afterwards: ./scripts/publish_wiki.sh
set -e
set -o pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIKI_URL="https://github.com/mansourvery-hub/CompreDef.wiki.git"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export GIT_TERMINAL_PROMPT=0
if ! git ls-remote "$WIKI_URL" HEAD >/dev/null 2>&1; then
    echo "Wiki repo not found — create the first page via the web UI once:"
    echo "  https://github.com/mansourvery-hub/CompreDef/wiki"
    exit 2
fi
git clone -q "$WIKI_URL" "$TMP/wiki"
cp "$PROJECT_ROOT/WIKI.md" "$TMP/wiki/Dictionary-Picker-Algorithm.md"
git -C "$TMP/wiki" add Dictionary-Picker-Algorithm.md
if git -C "$TMP/wiki" diff --cached --quiet; then
    echo "Wiki already up to date."
    exit 0
fi
git -C "$TMP/wiki" -c user.name=mansourvery-hub \
    -c user.email=mansourvery-hub@users.noreply.github.com \
    commit -qm "Sync Dictionary-Picker-Algorithm.md from repo WIKI.md"
git -C "$TMP/wiki" push -q origin master
echo "Wiki published."
