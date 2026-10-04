#!/bin/bash
# Das App-Ziel des erzeugten Xcode-Projekts auf manuelle Signatur umstellen.
#
#     tools/ios_signatur.sh <project.pbxproj> <Team-ID> <Profilname>
#
# **Warum hier und nicht als Parameter an xcodebuild.** Ein Build-Setting auf
# der Kommandozeile gilt fuer *jedes* Ziel, auch fuer die Swift-Pakete von
# Capacitor und den Plugins. Die kennen keine Bereitstellungsprofile, und
# Xcode bricht mit "does not support provisioning profiles" ab. Hier wird nur
# das App-Ziel angefasst.
#
# Die Vorlage setzt `CODE_SIGN_STYLE = Automatic;` genau zweimal - Debug und
# Release des App-Ziels. Stimmt die Zahl nicht, hat sich die Vorlage
# geaendert, und dann lieber abbrechen als irgendwo falsch zu signieren.
set -euo pipefail

PBXPROJ="${1:?project.pbxproj fehlt}"
TEAM="${2:?Team-ID fehlt}"
PROFIL="${3:?Profilname fehlt}"

ANZAHL="$(grep -c 'CODE_SIGN_STYLE = Automatic;' "$PBXPROJ" || true)"
if [ "$ANZAHL" != "2" ]; then
  echo "Erwartet: zwei Stellen mit 'CODE_SIGN_STYLE = Automatic;', gefunden: $ANZAHL." >&2
  echo "Die Capacitor-Vorlage hat sich geaendert - tools/ios_signatur.sh anpassen." >&2
  exit 1
fi

# Zeichen, die in der Ersetzung eine Bedeutung haetten, haben im Profilnamen
# nichts verloren. Apple erlaubt Leerzeichen und Bindestriche; mehr braucht es
# hier nicht.
case "$PROFIL" in
  *[\"\\\&\|/]*) echo "Profilname enthaelt ein unzulaessiges Zeichen: $PROFIL" >&2; exit 1 ;;
esac
case "$TEAM" in
  *[!A-Z0-9]*) echo "Team-ID darf nur aus Grossbuchstaben und Ziffern bestehen: $TEAM" >&2; exit 1 ;;
esac

# BSD-sed (macOS) verlangt hinter -i ein Argument, GNU-sed verbietet es. Mit
# einer Sicherungsdatei laufen beide.
sed -i.bak "s|CODE_SIGN_STYLE = Automatic;|CODE_SIGN_STYLE = Manual; DEVELOPMENT_TEAM = $TEAM; CODE_SIGN_IDENTITY = \"Apple Distribution\"; \"CODE_SIGN_IDENTITY[sdk=iphoneos*]\" = \"Apple Distribution\"; PROVISIONING_PROFILE_SPECIFIER = \"$PROFIL\";|" "$PBXPROJ"
rm -f "$PBXPROJ.bak"

echo "Signatur im App-Ziel gesetzt: Team $TEAM, Profil \"$PROFIL\"."
