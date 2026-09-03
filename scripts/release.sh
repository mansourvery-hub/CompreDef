#!/bin/bash
set -e

# CompreDef Release Script
# Builds the ready-to-install .ankiaddon package, runs regression tests,
# tags the git commit, pushes to remote, and publishes a GitHub Release with the .ankiaddon asset.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== [1/6] Running regression tests ==="
python3 tests/test_regression.py

# Determine version
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

echo "=== [2/6] Updating manifest.json ==="
python3 -c "
import json
manifest_path = 'manifest.json'
try:
    with open(manifest_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    data = {}
data['package'] = 'CompreDef'
data['name'] = 'CompreDef'
data['human_version'] = '$NUMERIC_VER'
with open(manifest_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)
    f.write('\n')
"

echo "=== [3/6] Packaging ready-to-install .ankiaddon ==="
mkdir -p dist
ANKIADDON_OUT="dist/CompreDef.ankiaddon"
rm -f "$ANKIADDON_OUT"

python3 -c "
import zipfile, os

files_to_include = [
    '__init__.py',
    'config.json',
    'manifest.json',
    'db_utils.py',
    'editor_browser.py',
    'generator.py',
    'gui.py',
    'parser.py',
    'README.md',
]

with zipfile.ZipFile('$ANKIADDON_OUT', 'w', compression=zipfile.ZIP_DEFLATED) as z:
    for rel_path in files_to_include:
        if os.path.exists(rel_path):
            z.write(rel_path, arcname=rel_path)
            print(f'  Added {rel_path}')
    if os.path.exists('icons'):
        for root, _, files in os.walk('icons'):
            for file in files:
                full_path = os.path.join(root, file)
                z.write(full_path, arcname=full_path)
                print(f'  Added {full_path}')
"

echo "=== [4/6] Verifying .ankiaddon structure ==="
python3 -c "
import zipfile, json, os
with zipfile.ZipFile('$ANKIADDON_OUT', 'r') as z:
    names = z.namelist()
    assert 'manifest.json' in names, 'Missing manifest.json'
    assert '__init__.py' in names, 'Missing __init__.py'
    assert 'config.json' in names, 'Missing config.json'
    with z.open('manifest.json') as f:
        m = json.load(f)
        assert m.get('package') == 'CompreDef', 'Invalid package name'
        assert m.get('name') == 'CompreDef', 'Invalid name'
print('Package verification PASSED (Size: ' + str(os.path.getsize('$ANKIADDON_OUT')) + ' bytes)')
"

echo "=== [5/6] Committing changes & pushing to GitHub ==="
git add VERSION manifest.json .gitignore scripts/
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

echo "=== [6/6] Creating GitHub Release with .ankiaddon attachment ==="
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
4. Open **Tools → Add-ons → CompreDef → Config** to configure your note fields and dictionary ladder."
fi

echo "======================================================================"
echo "SUCCESS: CompreDef $TAG release published with ready-to-install extension!"
echo "Release URL: $(gh release view "$TAG" --json url -q .url)"
echo "======================================================================"
