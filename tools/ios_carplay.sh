#!/bin/bash
# Die CarPlay-Szene in das erzeugte iOS-Projekt einbauen.
#
# Laeuft in der CI nach `cap add ios` und `cap sync`, wie ios_widget.sh. Der
# Swift-Code der Szene liegt im Plugin (plugins/jolt-anzeige,
# JoltCarPlaySceneDelegate); hier kommt, was das Projekt dafuer braucht:
#
#   1. Info.plist: eine zweite Szene fuer die Rolle
#      CPTemplateApplicationSceneSessionRoleApplication, mit dem Delegate des
#      Plugins - und UIApplicationSupportsMultipleScenes = true, ohne das
#      CarPlay keine eigene Szene bekommt.
#   2. AppDelegate.swift: Die Vorlage gibt **jeder** Szene den
#      Fenster-Delegate der Oberflaeche. Eine CarPlay-Szene mit einem
#      UIWindowSceneDelegate laeuft ins Leere; sie bekommt hier ihre eigene
#      Konfiguration aus der Info.plist.
#   3. Optional der Entitlement com.apple.developer.carplay-charging.
#
# **Warum der Entitlement ein Schalter ist.** Apple hat ihn dem Konto
# zugeteilt, aber er muss zusaetzlich fuer die App-ID im Developer-Portal
# eingeschaltet sein. Steht er in der App und nicht im Profil, bricht der
# signierte Bau ab. Ohne Entitlement baut die App wie bisher - die Szene ist
# dann vorhanden, aber CarPlay bietet sie nicht an.
#
#     bash tools/ios_carplay.sh [ios/App] [true|false]
set -euo pipefail

APP="${1:-ios/App}"
ENTITLEMENT="${2:-${CARPLAY_ENTITLEMENT:-false}}"
WURZEL="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$APP/App/Info.plist"
PB=/usr/libexec/PlistBuddy
ROLLE="CPTemplateApplicationSceneSessionRoleApplication"
KONFIG=":UIApplicationSceneManifest:UISceneConfigurations"

if [ ! -f "$PLIST" ]; then
  echo "Info.plist nicht gefunden: $PLIST" >&2
  exit 1
fi

# Mehrere Szenen: Pflicht fuer CarPlay. Der Schluessel steht in der Vorlage
# (als false); Set scheitert an einem fehlenden, Add an einem vorhandenen.
$PB -c "Set :UIApplicationSceneManifest:UIApplicationSupportsMultipleScenes true" "$PLIST" 2>/dev/null \
  || $PB -c "Add :UIApplicationSceneManifest:UIApplicationSupportsMultipleScenes bool true" "$PLIST"

$PB -c "Delete $KONFIG:$ROLLE" "$PLIST" 2>/dev/null || true
$PB -c "Add $KONFIG:$ROLLE array" "$PLIST"
$PB -c "Add $KONFIG:$ROLLE:0 dict" "$PLIST"
$PB -c "Add $KONFIG:$ROLLE:0:UISceneConfigurationName string CarPlay Configuration" "$PLIST"
$PB -c "Add $KONFIG:$ROLLE:0:UISceneDelegateClassName string JoltCarPlaySceneDelegate" "$PLIST"

echo "Info.plist: CarPlay-Szene eingetragen."
$PB -c "Print :UIApplicationSceneManifest" "$PLIST"

ruby "$WURZEL/tools/ios_carplay.rb" "$APP" "$ENTITLEMENT"
