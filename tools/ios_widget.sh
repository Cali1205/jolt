#!/bin/bash
# Die Live Activity (Widget-Erweiterung) in das erzeugte iOS-Projekt einbauen.
#
# Laeuft in der CI nach `cap add ios`, wie ios_info_plist.sh. Das Projekt ist
# nicht eingecheckt, die Swift-Quellen der Erweiterung schon: ios-native/ hat
# das Widget, plugins/jolt-anzeige/ das Plugin - und von dort kommt auch die
# gemeinsame Datei JoltFahrtAttributes.swift, damit Plugin und Widget
# dieselbe Beschreibung der Anzeige benutzen.
#
#     bash tools/ios_widget.sh [ios/App] [TEAM-ID]
set -euo pipefail

APP="${1:-ios/App}"
TEAM="${2:-${APPLE_TEAM_ID:-}}"
WURZEL="$(cd "$(dirname "$0")/.." && pwd)"
PROJEKT="$APP/App.xcodeproj"

if [ ! -d "$PROJEKT" ]; then
  echo "Xcode-Projekt nicht gefunden: $PROJEKT" >&2
  exit 1
fi

APP_ID="$(node -p "require('$WURZEL/capacitor.config.json').appId")"

ZIEL="$APP/JoltWidget"
mkdir -p "$ZIEL"
cp "$WURZEL/ios-native/JoltWidget/Info.plist" "$ZIEL/Info.plist"
cp "$WURZEL/ios-native/JoltWidget/JoltWidget.swift" "$ZIEL/JoltWidget.swift"
cp "$WURZEL/plugins/jolt-anzeige/ios/Sources/JoltAnzeigePlugin/JoltFahrtAttributes.swift" \
   "$ZIEL/JoltFahrtAttributes.swift"

# `xcodeproj` gehoert zu CocoaPods und liegt auf dem macOS-Laeufer. Fehlt es,
# wird es nachinstalliert - die Meldung "cannot load such file" allein sagt
# nicht, woran es liegt.
if ! ruby -e 'require "xcodeproj"' 2>/dev/null; then
  echo "xcodeproj fehlt, wird installiert ..."
  sudo gem install xcodeproj --no-document
fi

ruby "$WURZEL/tools/ios_widget.rb" "$PROJEKT" "$APP_ID" "$TEAM"
