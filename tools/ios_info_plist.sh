#!/bin/bash
# Add the missing entries to the Info.plist of the generated iOS project.
#
# `cap add ios` creates a project from a template, and the template knows
# nothing about Bluetooth. Two kinds of entries are therefore missing:
#
# **The usage descriptions.** If NSBluetoothAlwaysUsageDescription is missing,
# iOS terminates the app on the first CoreBluetooth access - with no error
# message in the app's log, because it is not the app that crashes but the
# system that removes it. This is the most unpleasant kind of bug: it looks
# like a crash in our own code.
#
# **The background modes.** `bluetooth-central` keeps the connection to the
# dongle while the app is not in the foreground; `location` is already
# entered here for the later step with background location, because a second
# pass through the plist editing would not make anything better.
#
# Runs in CI after `cap add ios`, see .github/workflows/ios.yml. The ios/
# directory is not checked in - it is created anew on every run, so these
# entries have to be set anew on every run.
set -euo pipefail

PLIST="${1:-ios/App/App/Info.plist}"

if [ ! -f "$PLIST" ]; then
  echo "Info.plist not found: $PLIST" >&2
  exit 1
fi

# Delete first, then create: `Add` fails on an existing key, and `Set` fails
# on a missing one. This order makes the script repeatable, whatever state the
# file is in.
set_text() {
  /usr/libexec/PlistBuddy -c "Delete :$1" "$PLIST" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :$1 string $2" "$PLIST"
}

set_text NSBluetoothAlwaysUsageDescription \
  "jolt liest ueber den OBD2-Adapter den Ladestand und die Zaehlerstaende aus dem Fahrzeug."
set_text NSBluetoothPeripheralUsageDescription \
  "jolt liest ueber den OBD2-Adapter den Ladestand und die Zaehlerstaende aus dem Fahrzeug."
set_text NSLocationWhenInUseUsageDescription \
  "jolt zeichnet die gefahrene Strecke auf und vergleicht sie mit der geplanten Route."
set_text NSLocationAlwaysAndWhenInUseUsageDescription \
  "jolt zeichnet die Fahrt weiter auf, waehrend das Telefon gesperrt ist."

# **Export compliance.** Without this key, App Store Connect asks about the
# encryption by hand for every uploaded build, and the build stays blocked
# for TestFlight until it is answered. jolt only uses what iOS ships anyway
# (HTTPS, CoreBluetooth) - that is exempt.
/usr/libexec/PlistBuddy -c "Delete :ITSAppUsesNonExemptEncryption" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :ITSAppUsesNonExemptEncryption bool false" "$PLIST"

# **Live Activities.** Without this key, ActivityKit rejects every request
# (Activity.request throws), and the display in CarPlay and on the lock
# screen stays empty - with no hint in the app's log.
/usr/libexec/PlistBuddy -c "Delete :NSSupportsLiveActivities" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSSupportsLiveActivities bool true" "$PLIST"

/usr/libexec/PlistBuddy -c "Delete :UIBackgroundModes" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :UIBackgroundModes array" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :UIBackgroundModes: string bluetooth-central" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :UIBackgroundModes: string location" "$PLIST"

echo "Info.plist updated:"
/usr/libexec/PlistBuddy -c "Print" "$PLIST" | grep -E \
  "NSBluetooth|NSLocation|NSSupportsLive|UIBackgroundModes|ITSApp" -A 2 || true
