#!/bin/bash
# Add the CarPlay scene to the generated iOS project.
#
# Runs in CI after `cap add ios` and `cap sync`, like ios_widget.sh. The
# scene's Swift code lives in the plugin (plugins/jolt-display,
# JoltCarPlaySceneDelegate); this script adds what the project needs for it:
#
#   1. Info.plist: a second scene for the role
#      CPTemplateApplicationSceneSessionRoleApplication, with the plugin's
#      delegate - and UIApplicationSupportsMultipleScenes = true, without
#      which CarPlay does not get a scene of its own.
#   2. AppDelegate.swift: the template gives **every** scene the UI's window
#      delegate. A CarPlay scene with a UIWindowSceneDelegate goes nowhere; it
#      gets its own configuration from the Info.plist here.
#   3. Optionally the entitlement com.apple.developer.carplay-charging.
#
# **Why the entitlement is a switch.** Apple has granted it to the account,
# but it must additionally be enabled for the app ID in the Developer Portal.
# If it is in the app but not in the profile, the signed build fails. Without
# the entitlement the app builds as before - the scene is then present, but
# CarPlay does not offer it.
#
#     bash tools/ios_carplay.sh [ios/App] [true|false]
set -euo pipefail

APP="${1:-ios/App}"
ENTITLEMENT="${2:-${CARPLAY_ENTITLEMENT:-false}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$APP/App/Info.plist"
PB=/usr/libexec/PlistBuddy
ROLE="CPTemplateApplicationSceneSessionRoleApplication"
CONFIGS=":UIApplicationSceneManifest:UISceneConfigurations"

if [ ! -f "$PLIST" ]; then
  echo "Info.plist not found: $PLIST" >&2
  exit 1
fi

# Multiple scenes: mandatory for CarPlay. The key is in the template (as
# false); Set fails on a missing key, Add on an existing one.
$PB -c "Set :UIApplicationSceneManifest:UIApplicationSupportsMultipleScenes true" "$PLIST" 2>/dev/null \
  || $PB -c "Add :UIApplicationSceneManifest:UIApplicationSupportsMultipleScenes bool true" "$PLIST"

$PB -c "Delete $CONFIGS:$ROLE" "$PLIST" 2>/dev/null || true
$PB -c "Add $CONFIGS:$ROLE array" "$PLIST"
$PB -c "Add $CONFIGS:$ROLE:0 dict" "$PLIST"
$PB -c "Add $CONFIGS:$ROLE:0:UISceneConfigurationName string CarPlay Configuration" "$PLIST"
$PB -c "Add $CONFIGS:$ROLE:0:UISceneDelegateClassName string JoltCarPlaySceneDelegate" "$PLIST"

echo "Info.plist: CarPlay scene added."
$PB -c "Print :UIApplicationSceneManifest" "$PLIST"

ruby "$ROOT/tools/ios_carplay.rb" "$APP" "$ENTITLEMENT"
