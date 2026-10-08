#!/bin/bash
# Replace the app icon of the generated iOS project with jolt's own.
#
# `cap add ios` creates the Capacitor logo as the icon. For TestFlight that
# is not just ugly: App Store Connect rejects the upload if the 1024 icon is
# missing or has an alpha channel. tools/ios-app-icon.png is therefore
# square, without rounded corners (iOS applies those itself) and without
# transparency - tools/check_ios.py checks exactly that.
#
# Derived from frontend/icon.svg; regenerate it when that changes.
set -euo pipefail

TARGET="${1:-ios/App/App/Assets.xcassets/AppIcon.appiconset}"
SOURCE="$(cd "$(dirname "$0")" && pwd)/ios-app-icon.png"

if [ ! -d "$TARGET" ]; then
  echo "AppIcon directory not found: $TARGET" >&2
  exit 1
fi

# The file name is in the template's Contents.json. Read it from there
# instead of hard-wiring it: if Capacitor changes it, the icon should still
# end up in the right place.
NAME="$(sed -n 's/.*"filename" *: *"\([^"]*\)".*/\1/p' "$TARGET/Contents.json" | head -n 1)"
if [ -z "$NAME" ]; then
  echo "Contents.json names no icon file: $TARGET/Contents.json" >&2
  exit 1
fi

cp "$SOURCE" "$TARGET/$NAME"
echo "App icon replaced: $TARGET/$NAME"
