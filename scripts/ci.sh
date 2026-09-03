#!/bin/bash
set -e

echo "Running regression tests..."
python3 tests/test_regression.py

echo "Committing and pushing changes..."
git add .
# Only commit if there are changes to avoid empty commit errors
if git diff-index --quiet HEAD --; then
    echo "No changes to commit"
else
    git commit -m "Auto-commit: regression tests passed"
    git push origin master
fi
