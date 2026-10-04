#!/bin/bash
# Das App-Symbol des erzeugten iOS-Projekts durch das von jolt ersetzen.
#
# `cap add ios` legt das Capacitor-Logo als Symbol an. Fuer TestFlight ist
# das nicht nur haesslich: App Store Connect lehnt den Upload ab, wenn das
# 1024er-Symbol fehlt oder einen Alphakanal hat. tools/ios-app-icon.png ist
# deshalb quadratisch, ohne abgerundete Ecken (die setzt iOS selbst) und
# ohne Transparenz - tools/check_ios.py prueft genau das.
#
# Entstanden aus frontend/icon.svg; neu erzeugen, wenn sich das aendert.
set -euo pipefail

ZIEL="${1:-ios/App/App/Assets.xcassets/AppIcon.appiconset}"
QUELLE="$(cd "$(dirname "$0")" && pwd)/ios-app-icon.png"

if [ ! -d "$ZIEL" ]; then
  echo "AppIcon-Verzeichnis nicht gefunden: $ZIEL" >&2
  exit 1
fi

# Der Dateiname steht in der Contents.json der Vorlage. Aus ihr lesen statt
# fest verdrahten: Aendert Capacitor ihn, soll das Symbol trotzdem landen.
NAME="$(sed -n 's/.*"filename" *: *"\([^"]*\)".*/\1/p' "$ZIEL/Contents.json" | head -n 1)"
if [ -z "$NAME" ]; then
  echo "Contents.json nennt keine Symboldatei: $ZIEL/Contents.json" >&2
  exit 1
fi

cp "$QUELLE" "$ZIEL/$NAME"
echo "App-Symbol ersetzt: $ZIEL/$NAME"
