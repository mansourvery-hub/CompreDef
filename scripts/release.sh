#!/bin/bash
set -e

echo "Running regression tests before release..."
python3 tests/test_regression.py

# Get current version from some source or ask user
# For now, we'll use a simple prompt or a VERSION file if it exists
if [ -f VERSION ]; then
    VERSION=$(cat VERSION)
else
    read -p "Enter release version (e.g. v1.0.0): " VERSION
fi

echo "Creating release $VERSION..."
git add .
git commit -m "Release $VERSION"
git tag -a "$VERSION" -m "Release $VERSION"
git push origin master
git push origin "$VERSION"

echo "Release $VERSION complete."
