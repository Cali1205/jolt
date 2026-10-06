#!/bin/bash
# Die Info.plist des erzeugten iOS-Projekts ergaenzen.
#
# `cap add ios` legt ein Projekt aus einer Vorlage an, und die Vorlage weiss
# nichts von Bluetooth. Zwei Sorten Eintraege fehlen deshalb:
#
# **Die Verwendungszwecke.** Fehlt NSBluetoothAlwaysUsageDescription, beendet
# iOS die App beim ersten CoreBluetooth-Zugriff - ohne Fehlermeldung im
# Protokoll der App, weil nicht sie abstuerzt, sondern das System sie
# abraeumt. Das ist die unangenehmste Sorte Fehler: Er sieht wie ein Absturz
# im eigenen Code aus.
#
# **Die Hintergrundmodi.** `bluetooth-central` haelt die Verbindung zum
# Dongle, waehrend die App nicht im Vordergrund ist; `location` ist fuer den
# spaeteren Schritt mit dem Hintergrund-Standort schon hier eingetragen, weil
# ein zweiter Durchgang durch die Plist-Bearbeitung nichts besser machte.
#
# Laeuft in der CI nach `cap add ios`, siehe .github/workflows/ios.yml. Das
# Verzeichnis ios/ ist nicht eingecheckt - es entsteht bei jedem Lauf neu,
# und damit muessen diese Eintraege bei jedem Lauf neu gesetzt werden.
set -euo pipefail

PLIST="${1:-ios/App/App/Info.plist}"

if [ ! -f "$PLIST" ]; then
  echo "Info.plist nicht gefunden: $PLIST" >&2
  exit 1
fi

# Erst loeschen, dann anlegen: `Add` scheitert an einem vorhandenen
# Schluessel, und ein `Set` scheitert an einem fehlenden. Die Reihenfolge
# macht das Skript wiederholbar, egal welchen Stand die Datei hat.
setze_text() {
  /usr/libexec/PlistBuddy -c "Delete :$1" "$PLIST" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$PLIST"
}

setze_text NSBluetoothAlwaysUsageDescription \
  "jolt liest ueber den OBD2-Adapter den Ladestand und die Zaehlerstaende aus dem Fahrzeug."
setze_text NSBluetoothPeripheralUsageDescription \
  "jolt liest ueber den OBD2-Adapter den Ladestand und die Zaehlerstaende aus dem Fahrzeug."
setze_text NSLocationWhenInUseUsageDescription \
  "jolt zeichnet die gefahrene Strecke auf und vergleicht sie mit der geplanten Route."
setze_text NSLocationAlwaysAndWhenInUseUsageDescription \
  "jolt zeichnet die Fahrt weiter auf, waehrend das Telefon gesperrt ist."

# **Ausfuhrbestimmungen.** Ohne diesen Schluessel fragt App Store Connect bei
# jedem hochgeladenen Bau von Hand nach der Verschluesselung, und der Bau
# bleibt bis zur Antwort fuer TestFlight gesperrt. jolt benutzt nur das, was
# iOS ohnehin mitbringt (HTTPS, CoreBluetooth) - das ist ausgenommen.
/usr/libexec/PlistBuddy -c "Delete :ITSAppUsesNonExemptEncryption" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :ITSAppUsesNonExemptEncryption bool false" "$PLIST"

# **Live Activities.** Ohne diesen Schluessel lehnt ActivityKit jede Anfrage ab
# (Activity.request wirft), und die Anzeige in CarPlay und auf dem
# Sperrbildschirm bleibt leer - ohne Hinweis im Protokoll der App.
/usr/libexec/PlistBuddy -c "Delete :NSSupportsLiveActivities" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSSupportsLiveActivities bool true" "$PLIST"

/usr/libexec/PlistBuddy -c "Delete :UIBackgroundModes" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :UIBackgroundModes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :UIBackgroundModes: string bluetooth-central" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :UIBackgroundModes: string location" "$PLIST"

echo "Info.plist ergaenzt:"
/usr/libexec/PlistBuddy -c "Print" "$PLIST" | grep -E \
  "NSBluetooth|NSLocation|NSSupportsLive|UIBackgroundModes|ITSApp" -A 2 || true
