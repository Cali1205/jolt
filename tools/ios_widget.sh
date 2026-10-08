#!/bin/bash
# Add the Live Activity (widget extension) to the generated iOS project.
#
# Runs in CI after `cap add ios`, like ios_info_plist.sh. The project is not
# checked in, but the extension's Swift sources are: ios-native/ has the
# widget, plugins/jolt-display/ the plugin - and the shared file
# JoltTripAttributes.swift also comes from there, so that plugin and widget
# use the same description of the display.
#
#     bash tools/ios_widget.sh [ios/App] [TEAM-ID]
set -euo pipefail

APP="${1:-ios/App}"
TEAM="${2:-${APPLE_TEAM_ID:-}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="$APP/App.xcodeproj"

if [ ! -d "$PROJECT" ]; then
  echo "Xcode project not found: $PROJECT" >&2
  exit 1
fi

APP_ID="$(node -p "require('$ROOT/capacitor.config.json').appId")"

TARGET="$APP/JoltWidget"
mkdir -p "$TARGET"
cp "$ROOT/ios-native/JoltWidget/Info.plist" "$TARGET/Info.plist"
cp "$ROOT/ios-native/JoltWidget/JoltWidget.swift" "$TARGET/JoltWidget.swift"
cp "$ROOT/plugins/jolt-display/ios/Sources/JoltDisplayPlugin/JoltTripAttributes.swift" \
   "$TARGET/JoltTripAttributes.swift"

# `xcodeproj` belongs to CocoaPods and is present on the macOS runner. If it
# is missing, it is installed on the fly - the message "cannot load such
# file" alone does not say what is wrong.
if ! ruby -e 'require "xcodeproj"' 2>/dev/null; then
  echo "xcodeproj missing, installing ..."
  sudo gem install xcodeproj --no-document
fi

ruby "$ROOT/tools/ios_widget.rb" "$PROJECT" "$APP_ID" "$TEAM"
