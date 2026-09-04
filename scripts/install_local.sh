#!/bin/bash
set -e
set -o pipefail

# CompreDef Local Install Script
# Installs the freshly built dist/CompreDef.ankiaddon directly into the
# local Anki add-ons folder so the user can test without manual
# "Install from file..." and without waiting for AnkiWeb.
#
# This is the final step of the pipeline — after tests, build, and
# (for ci.sh/release.sh) GitHub + AnkiWeb upload. The add-on is already
# on AnkiWeb, but a local install is instant and does not require a
# double-restart. The user just restarts Anki once to see the change.
#
# Usage:
#   ./scripts/install_local.sh              # uses dist/CompreDef.ankiaddon
#   ./scripts/install_local.sh /path/to.ankiaddon
#
# Exit code 0 = installed, 1 = failed (with reason printed).

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANKIADDON="${1:-$PROJECT_ROOT/dist/CompreDef.ankiaddon}"

# Detect Anki add-ons folder per platform
detect_anki_addons_dir() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "$HOME/Library/Application Support/Anki2/addons21"
    elif [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* || "$OSTYPE" == "win32"* ]]; then
        # Git Bash / MSYS2 on Windows
        if [[ -n "$APPDATA" ]]; then
            echo "$APPDATA/Anki2/addons21"
        else
            echo "$USERPROFILE/AppData/Roaming/Anki2/addons21"
        fi
    else
        # Linux / WSL
        echo "$HOME/.local/share/Anki2/addons21"
    fi
}

ADDONS21="$(detect_anki_addons_dir)"
TARGET_DIR="$ADDONS21/1619602654"

if [[ ! -f "$ANKIADDON" ]]; then
    echo "!!! install_local.sh: $ANKIADDON not found. Run ./scripts/build.sh first."
    exit 1
fi

# Verify it's a valid zip with manifest
if ! unzip -l "$ANKIADDON" | grep -q "manifest.json"; then
    echo "!!! install_local.sh: $ANKIADDON is not a valid .ankiaddon (missing manifest.json)"
    exit 1
fi

echo "=== Installing CompreDef locally ==="
echo "Source: $ANKIADDON ($(du -h "$ANKIADDON" | cut -f1))"
echo "Target: $TARGET_DIR"

mkdir -p "$TARGET_DIR"

# Preserve user_files and meta.json — they contain the user's dictionaries,
# yomitan_bridge, and the add-on's display name (human_name) + config.
# meta.json must survive because Anki 26.08 stores `name` and `human_version`
# there (copied from manifest.json on install). Deleting it makes the add-on
# appear as its folder ID (1619602654) instead of "CompreDef".
PRESERVE_DIR="$(mktemp -d)"
USER_FILES_PRESERVED=false
META_PRESERVED=false
if [[ -d "$TARGET_DIR/user_files" ]]; then
    echo "Preserving $TARGET_DIR/user_files ..."
    cp -a "$TARGET_DIR/user_files" "$PRESERVE_DIR/" 2>/dev/null && USER_FILES_PRESERVED=true || true
fi
if [[ -f "$TARGET_DIR/meta.json" ]]; then
    echo "Preserving $TARGET_DIR/meta.json ..."
    cp -a "$TARGET_DIR/meta.json" "$PRESERVE_DIR/" 2>/dev/null && META_PRESERVED=true || true
fi

# Clean old code but keep user_files/meta.json aside
if [[ -d "$TARGET_DIR" ]]; then
    find "$TARGET_DIR" -mindepth 1 -maxdepth 1 ! -name "user_files" ! -name "meta.json" -exec rm -rf {} + 2>/dev/null || true
fi

echo "Unzipping $ANKIADDON -> $TARGET_DIR ..."
unzip -o -q "$ANKIADDON" -d "$TARGET_DIR"

if [[ "$USER_FILES_PRESERVED" == true ]]; then
    echo "Restoring user_files ..."
    rm -rf "$TARGET_DIR/user_files"
    cp -a "$PRESERVE_DIR/user_files" "$TARGET_DIR/" 2>/dev/null || true
fi
if [[ "$META_PRESERVED" == true ]]; then
    echo "Restoring meta.json ..."
    if [[ -f "$PRESERVE_DIR/meta.json" ]]; then
        cp -a "$PRESERVE_DIR/meta.json" "$TARGET_DIR/meta.json" 2>/dev/null || true
        # Patch name/human_version from new manifest.json into the restored meta.json
        python3 - <<'PYEOF' 2>/dev/null || true
import json, os
target = os.path.expanduser("~/.local/share/Anki2/addons21/1619602654")
if not os.path.isdir(target):
    home = os.path.expanduser("~")
    for cand in [
        os.path.join(home, "Library/Application Support/Anki2/addons21/1619602654"),
        os.path.join(os.getenv("APPDATA", ""), "Anki2/addons21/1619602654"),
    ]:
        if os.path.isdir(cand):
            target = cand
            break
try:
    with open(os.path.join(target, "manifest.json"), "r", encoding="utf-8") as mf:
        mani = json.load(mf)
    with open(os.path.join(target, "meta.json"), "r", encoding="utf-8") as mf:
        meta = json.load(mf)
    for key in ("name", "human_version", "package"):
        if key in mani:
            meta[key] = mani[key]
    if "name" not in meta or not meta["name"]:
        meta["name"] = "CompreDef"
    with open(os.path.join(target, "meta.json"), "w", encoding="utf-8") as out:
        json.dump(meta, out, indent=4)
        out.write("\n")
    print(f"Patched meta.json name={meta.get('name')} human_version={meta.get('human_version')}")
except Exception as e:
    print(f"meta.json patch failed: {e}")
PYEOF
    fi
fi
rm -rf "$PRESERVE_DIR"

# Verify install
if [[ ! -f "$TARGET_DIR/__init__.py" ]]; then
    echo "!!! install_local.sh: install verification failed — __init__.py missing in $TARGET_DIR"
    exit 1
fi
if [[ ! -f "$TARGET_DIR/manifest.json" ]]; then
    echo "!!! install_local.sh: install verification failed — manifest.json missing"
    exit 1
fi

echo "Local install OK: $TARGET_DIR"
echo "  $(ls -1 "$TARGET_DIR"/*.py 2>/dev/null | wc -l) Python modules, manifest $(cat "$TARGET_DIR/manifest.json" | tr -d '\n' | cut -c1-80)..."
echo ""
echo "Restart Anki once to load the new code (no double-restart needed — this is already local)."
echo "If Anki was running, close it fully (check system tray) then reopen."
