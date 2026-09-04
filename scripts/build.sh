#!/bin/bash
set -e
set -o pipefail

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
# On test failure the [FAIL] lines are reprinted at the bottom so the
# actual failing checks are impossible to miss (a plain
# "113/114 passed, 1 failed" summary forced a scroll-back hunt).

# Report exactly WHICH command failed instead of a bare silent exit.
trap 'echo ""; echo "!!! build.sh FAILED at: $BASH_COMMAND (line $LINENO)"' ERR

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== [1/4] Running regression tests ==="
# Stream live output AND capture it, so failures can be re-summarized.
TEST_LOG="$(mktemp)"
if python3 tests/test_regression.py 2>&1 | tee "$TEST_LOG"; then
    rm -f "$TEST_LOG"
else
    echo ""
    echo "!!! REGRESSION TESTS FAILED — failing checks:"
    grep -E '^\[FAIL\]' "$TEST_LOG" || echo "  (no [FAIL] lines captured — see full output above)"
    rm -f "$TEST_LOG"
    exit 1
fi

echo "=== [2/4] Packaging ready-to-install .ankiaddon ==="
mkdir -p dist
rm -f dist/CompreDef.ankiaddon

python3 - <<'PYEOF'
import zipfile, os, glob

# Ship EVERY root-level module plus fixed assets. A hardcoded file list
# went stale after the module refactor and produced an .ankiaddon missing
# provider.py/renderer.py/core.py/... — an instant ImportError on every
# user's machine. A glob can never go stale.
py_files = sorted(glob.glob("*.py"))
assets = ["config.json", "manifest.json", "README.md"]

with zipfile.ZipFile("dist/CompreDef.ankiaddon", "w", compression=zipfile.ZIP_DEFLATED) as z:
    for rel_path in py_files + assets:
        if os.path.exists(rel_path):
            z.write(rel_path, arcname=rel_path)
            print(f"  Added {rel_path}")
    if os.path.exists("icons"):
        for root, _, files in os.walk("icons"):
            for file in files:
                full_path = os.path.join(root, file)
                z.write(full_path, arcname=full_path)
                print(f"  Added {full_path}")
PYEOF

echo "=== [3/4] Verifying .ankiaddon structure ==="
python3 - <<'PYEOF'
import zipfile, json, os

with zipfile.ZipFile("dist/CompreDef.ankiaddon", "r") as z:
    names = z.namelist()
    assert "manifest.json" in names, "Missing manifest.json"
    assert "__init__.py" in names, "Missing __init__.py"
    assert "config.json" in names, "Missing config.json"
    # Every runtime module must ship. This assertion is the tripwire for
    # the historical bug where refactored modules were left out of the
    # package and the add-on crashed with ImportError on the user machine.
    REQUIRED = [
        "__init__.py", "core.py", "engine.py", "provider.py", "renderer.py",
        "models.py", "scoring.py", "utils.py", "anki.py", "parser.py",
        "generator.py", "gui.py", "editor_browser.py",
    ]
    missing = [m for m in REQUIRED if m not in names]
    assert not missing, f"Missing runtime modules: {missing}"
    with z.open("manifest.json") as f:
        m = json.load(f)
        # Package MUST be the AnkiWeb ID: Anki installs the folder under
        # that name, and only a numeric folder name enables the native
        # 'View Add-on Page' button in the Add-ons manager.
        assert m.get("package") == "1619602654", \
            "Invalid package name: " + repr(m.get("package"))
        assert m.get("name") == "CompreDef", "Invalid name"
print("Package verification PASSED (Size: " + str(os.path.getsize("dist/CompreDef.ankiaddon")) + " bytes)")
PYEOF

echo "======================================================================"
echo "Built: dist/CompreDef.ankiaddon"
echo "Install via: Anki → Tools → Add-ons → Install from file..."
echo "======================================================================"

# --- Auto local install (last step, never fails the build) ---
if [[ -f "dist/CompreDef.ankiaddon" ]]; then
    echo ""
    echo "=== [4/4] Auto local install (for immediate testing) ==="
    if ./scripts/install_local.sh 2>&1; then
        echo "Auto local install: OK"
    else
        echo "Auto local install: FAILED (non-fatal — build is still OK, install manually via Anki)"
    fi
fi
