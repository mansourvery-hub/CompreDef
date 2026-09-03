#!/bin/bash
set -e

# CompreDef Build Script
# Builds dist/CompreDef.ankiaddon — the ready-to-install package — WITHOUT
# any git or release side effects. Use this after any code change to test
# locally in Anki:
#
#   1. ./scripts/build.sh
#   2. Anki → Tools → Add-ons → Install from file...
#      → select dist/CompreDef.ankiaddon
#   3. Restart Anki
#
# Regression tests are run first: a broken build must never be packaged.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== [1/3] Running regression tests ==="
python3 tests/test_regression.py

echo "=== [2/3] Packaging ready-to-install .ankiaddon ==="
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

echo "=== [3/3] Verifying .ankiaddon structure ==="
python3 -c "
import zipfile, json, os
with zipfile.ZipFile('$ANKIADDON_OUT', 'r') as z:
    names = z.namelist()
    assert 'manifest.json' in names, 'Missing manifest.json'
    assert '__init__.py' in names, 'Missing __init__.py'
    assert 'config.json' in names, 'Missing config.json'
    with z.open('manifest.json') as f:
        m = json.load(f)
        # Package MUST be the AnkiWeb ID: Anki installs the folder under
        # that name, and only a numeric folder name enables the native
        # 'View Add-on Page' button in the Add-ons manager.
        assert m.get('package') == '1619602654', \
            'Invalid package name: ' + repr(m.get('package'))
        assert m.get('name') == 'CompreDef', 'Invalid name'
print('Package verification PASSED (Size: ' + str(os.path.getsize('$ANKIADDON_OUT')) + ' bytes)')
"

echo "======================================================================"
echo "Built: $ANKIADDON_OUT"
echo "Install via: Anki → Tools → Add-ons → Install from file..."
echo "======================================================================"
