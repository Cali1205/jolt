#!/usr/bin/env python3
"""Prüft die iOS-Auslieferung, soweit sie sich ohne Mac prüfen lässt.

Der signierte Bau läuft nur auf einem macOS-Läufer und mit Apple-Konto. Was
vorher auffallen kann, soll aber vorher auffallen - der erste iOS-Lauf auf
`main` ist an einer App-ID mit Bindestrich gescheitert, die Capacitor
ablehnt, und das hätte ein Blick in diese Datei gesehen. Es kostet nichts,
und ein macOS-Lauf kostet das Zehnfache an Minuten.

Ohne Netz, ohne Datenbank, ohne Apple:

    ./tools/check_ios.py
"""
import json
import os
import re
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung  # noqa: E402

WURZEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pruefe = Pruefung()


def lesen(*teile: str) -> str:
    with open(os.path.join(WURZEL, *teile), encoding="utf-8") as datei:
        return datei.read()


def teil_app_id() -> None:
    pruefe.abschnitt("App-ID")
    konfig = json.loads(lesen("capacitor.config.json"))
    app_id = konfig.get("appId", "")
    # Die Regel aus @capacitor/cli (validateAppId): Java-Paketform, kein
    # Bindestrich, jedes Segment beginnt mit einem Buchstaben. Nachgebaut,
    # weil `cap add ios` das erst auf dem Mac-Läufer prüft.
    segmente = app_id.split(".")
    gueltig = (len(segmente) >= 2
               and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", s)
                       for s in segmente))
    pruefe(gueltig,
           "die App-ID erfüllt Capacitors Regeln - sonst bricht `cap add ios` "
           "ab, ehe überhaupt etwas gebaut wird", app_id)
    doku = lesen("ios-einrichten.md")
    pruefe(app_id in doku,
           "und die Einrichtungsanleitung nennt genau diese ID", app_id)
    alte = [d for d in ("konzept-ios-app.md", "ios-einrichten.md")
            if "the-smarthome.jolt" in lesen(d)]
    pruefe(not alte,
           "keine Anleitung nennt mehr eine ID, die der Konfiguration "
           "widerspricht", str(alte))


def teil_symbol() -> None:
    pruefe.abschnitt("App-Symbol")
    with open(os.path.join(WURZEL, "tools", "ios-app-icon.png"), "rb") as f:
        kopf = f.read(33)
    breite, hoehe, tiefe, farbtyp = struct.unpack(">IIBB", kopf[16:26])
    pruefe(kopf[:8] == b"\x89PNG\r\n\x1a\n", "das App-Symbol ist eine PNG-Datei")
    pruefe((breite, hoehe) == (1024, 1024),
           "und genau 1024 × 1024 Pixel gross - mehr verlangt App Store "
           "Connect, weniger lehnt es ab", f"{breite}×{hoehe}")
    # Farbtyp 2 = RGB, 3 = Palette; 4 und 6 haben einen Alphakanal. Apple
    # weist solche Symbole beim Upload ab ("Invalid large app icon").
    pruefe(farbtyp in (0, 2, 3) and tiefe == 8,
           "ohne Alphakanal - Transparenz im großen Symbol lehnt Apple ab",
           f"Farbtyp {farbtyp}")


def teil_skripte() -> None:
    pruefe.abschnitt("Skripte")
    for name in ("ios_info_plist.sh", "ios_symbol.sh", "ios_signatur.sh"):
        ergebnis = subprocess.run(
            ["bash", "-n", os.path.join(WURZEL, "tools", name)],
            capture_output=True, text=True)
        pruefe(ergebnis.returncode == 0,
               f"{name} hat keinen Syntaxfehler", ergebnis.stderr.strip())

    plist = lesen("tools", "ios_info_plist.sh")
    for schluessel in ("NSBluetoothAlwaysUsageDescription",
                       "NSLocationWhenInUseUsageDescription",
                       "ITSAppUsesNonExemptEncryption"):
        pruefe(schluessel in plist,
               f"die Info.plist bekommt {schluessel} - ohne ihn räumt iOS "
               f"die App ab oder TestFlight hält jeden Bau an")
    pruefe("bluetooth-central" in plist,
           "und den Hintergrundmodus für die Dongle-Verbindung")


def teil_signatur() -> None:
    """ios_signatur.sh gegen eine Attrappe der Capacitor-Vorlage laufen lassen.

    Die Skripte sind portabel geschrieben (sed mit Sicherungsdatei), also
    läuft das auch hier. Geprüft wird vor allem das Verweigern: Ein Skript,
    das bei veränderter Vorlage still weitermacht, signiert falsch.
    """
    pruefe.abschnitt("Signatur-Skript")
    import tempfile
    skript = os.path.join(WURZEL, "tools", "ios_signatur.sh")
    vorlage = ("\t\tbuildSettings = {\n\t\t\tCODE_SIGN_STYLE = Automatic;\n\t\t};\n"
               "\t\tbuildSettings = {\n\t\t\tCODE_SIGN_STYLE = Automatic;\n\t\t};\n")

    def lauf(inhalt: str, team: str = "ABCDE12345", profil: str = "jolt AppStore"):
        with tempfile.TemporaryDirectory() as ordner:
            pfad = os.path.join(ordner, "project.pbxproj")
            with open(pfad, "w", encoding="utf-8") as f:
                f.write(inhalt)
            r = subprocess.run(["bash", skript, pfad, team, profil],
                               capture_output=True, text=True)
            with open(pfad, encoding="utf-8") as f:
                return r.returncode, f.read(), r.stderr

    rc, text, _ = lauf(vorlage)
    pruefe(rc == 0 and text.count("CODE_SIGN_STYLE = Manual;") == 2
           and "Automatic" not in text,
           "beide Konfigurationen des App-Ziels werden auf Manuell gestellt")
    pruefe(text.count('PROVISIONING_PROFILE_SPECIFIER = "jolt AppStore";') == 2
           and text.count("DEVELOPMENT_TEAM = ABCDE12345;") == 2,
           "mit Team und Profilname")
    rc, text, _ = lauf(vorlage + "\t\t\tCODE_SIGN_STYLE = Automatic;\n")
    pruefe(rc != 0 and "Manual" not in text,
           "ändert sich die Vorlage (drei statt zwei Stellen), bricht das "
           "Skript ab, statt irgendwo falsch zu signieren")
    rc, _, _ = lauf(vorlage, team="ab; rm -rf")
    pruefe(rc != 0, "eine Team-ID mit Sonderzeichen wird abgelehnt")
    rc, _, _ = lauf(vorlage, profil='x" ; evil')
    pruefe(rc != 0, "ein Profilname mit Anführungszeichen ebenso")


def teil_workflows() -> None:
    pruefe.abschnitt("Workflows")
    ios = lesen(".github", "workflows", "ios.yml")
    fliegen = lesen(".github", "workflows", "ios-testflight.yml")

    # Capacitor 8 nimmt den Swift Package Manager: Es gibt keine
    # .xcworkspace, und ein `-workspace` ist ein Bau, der nie anläuft.
    for name, text in (("ios.yml", ios), ("ios-testflight.yml", fliegen)):
        ohne_kommentare = "\n".join(
            z for z in text.splitlines() if not z.lstrip().startswith("#"))
        pruefe("-workspace" not in ohne_kommentare
               and "pod " not in ohne_kommentare,
               f"{name} baut ohne CocoaPods-Workspace - Capacitor 8 nimmt "
               f"Swift-Pakete")
    pruefe("-project ios/App/App.xcodeproj" in ios
           and "-project ios/App/App.xcodeproj" in fliegen,
           "beide Abläufe bauen das Projekt, das `cap add ios` anlegt")

    # Der Simulatorbau darf nicht signieren, der Archivbau muss.
    pruefe("CODE_SIGNING_ALLOWED=NO" in ios,
           "der Simulatorbau läuft unsigniert - er braucht kein Konto")
    pruefe("archive" in fliegen and "-exportArchive" in fliegen
           and "destination</key><string>upload" in fliegen,
           "der TestFlight-Ablauf archiviert, exportiert und lädt hoch")

    # Beide Abläufe müssen dasselbe Projekt erzeugen, sonst baut der eine
    # etwas anderes, als der andere ausliefert.
    for schritt in ("cap add ios", "tools/ios_info_plist.sh",
                    "tools/ios_symbol.sh", "cap sync ios"):
        pruefe(schritt in ios and schritt in fliegen,
               f"beide Abläufe führen `{schritt}` aus")

    # Jedes referenzierte Geheimnis muss in der Anleitung stehen - sonst
    # steht in der Fehlermeldung ein Name, den niemand erklärt.
    doku = lesen("ios-einrichten.md")
    geheimnisse = sorted(set(re.findall(r"secrets\.([A-Z0-9_]+)", fliegen)))
    pruefe(len(geheimnisse) == 7, "der Ablauf braucht sieben Geheimnisse",
           str(geheimnisse))
    fehlen = [g for g in geheimnisse if f"`{g}`" not in doku]
    pruefe(not fehlen,
           "und die Anleitung erklärt jedes davon", str(fehlen))

    pruefe(re.search(r"^on:\s*\n\s+workflow_dispatch:", fliegen, re.M)
           and "pull_request" not in fliegen,
           "der Upload läuft nur von Hand oder per Tag - nie auf Pull "
           "Requests, wo Geheimnisse nicht zur Verfügung stehen und ein "
           "Mac-Lauf zehnfach zählt")
    pruefe("frontend/**" not in ios,
           "frontend/ löst keinen App-Bau aus - die Oberfläche kommt zur "
           "Laufzeit vom Server")
    pruefe("if: always()" in fliegen and "delete-keychain" in fliegen,
           "der flüchtige Schlüsselbund wird auch nach einem Fehler gelöscht")


def main() -> int:
    teil_app_id()
    teil_symbol()
    teil_skripte()
    teil_signatur()
    teil_workflows()
    return pruefe.bilanz(
        "Nicht geprüft (nur mit Apple-Konto und Mac-Läufer prüfbar): ob das "
        "Zertifikat zum Profil passt, ob xcodebuild das Projekt signiert und "
        "ob App Store Connect den Upload annimmt. Das zeigt der erste Lauf "
        "von ios-testflight.yml.")


if __name__ == "__main__":
    sys.exit(main())
