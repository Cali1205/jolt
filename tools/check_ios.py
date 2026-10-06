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
    for name in ("ios_info_plist.sh", "ios_symbol.sh", "ios_signatur.sh",
                 "ios_widget.sh"):
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
                    "tools/ios_symbol.sh", "tools/ios_widget.sh",
                    "cap sync ios"):
        pruefe(schritt in ios and schritt in fliegen,
               f"beide Abläufe führen `{schritt}` aus")

    # Jedes referenzierte Geheimnis muss in der Anleitung stehen - sonst
    # steht in der Fehlermeldung ein Name, den niemand erklärt.
    doku = lesen("ios-einrichten.md")
    geheimnisse = sorted(set(re.findall(r"secrets\.([A-Z0-9_]+)", fliegen)))
    pruefe(len(geheimnisse) == 7,
           "der Ablauf kennt sieben Geheimnisse: vier für den automatischen "
           "Weg, drei weitere für den manuellen", str(geheimnisse))
    pruefe("MODUS=automatisch" in fliegen and "MODUS=manuell" in fliegen
           and "-allowProvisioningUpdates" in fliegen,
           "und wählt danach: ohne Zertifikat und Profil signiert Apple "
           "selbst, mit beiden die mitgelieferten")
    pruefe("CODE_SIGNING_ALLOWED=NO" in fliegen,
           "im automatischen Modus wird unsigniert archiviert - beim "
           "Archivieren verlangt Xcode sonst ein Entwicklungsprofil und damit "
           "ein registriertes Gerät (der erste echte Lauf scheiterte daran)")
    pruefe("EXTRA[@]+" in fliegen,
           "leere Arrays werden mit set -u auf dem alten bash des Mac-Läufers "
           "sicher aufgelöst - sonst bricht der Export im manuellen Modus ab")
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


def swift_felder(text: str) -> dict:
    """Die Felder jeder `struct` in einer Swift-Datei: Name -> {Feld: optional?}.

    Reicht für die schlichten Wertetypen dieser Datei; ein Parser für
    Swift ist es nicht."""
    ergebnis = {}
    for treffer in re.finditer(r"struct (\w+)[^{]*\{", text):
        tiefe, i = 1, treffer.end()
        while i < len(text) and tiefe:
            tiefe += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        rumpf = text[treffer.end():i]
        # Geschachtelte Strukturen gehören nicht zu den Feldern der äusseren.
        flach = re.sub(r"struct \w+[^{]*\{[^{}]*\}", "", rumpf)
        ergebnis[treffer.group(1)] = {
            m.group(1): m.group(2).endswith("?")
            for m in re.finditer(r"public var (\w+): ([\w.]+\??)", flach)}
    return ergebnis


def teil_live_activity() -> None:
    """Plugin, Widget und Anzeigemodell müssen zusammenpassen.

    Ob die Live Activity in CarPlay erscheint, zeigt nur ein Gerät. Was sich
    ohne Mac prüfen lässt: dass die Swift-Seite jedes Pflichtfeld, das sie
    liest, von der JavaScript-Seite auch bekommt - fehlt eines, scheitert das
    Lesen des Modells, und die Anzeige bleibt leer, ohne dass etwas abstürzt.
    """
    pruefe.abschnitt("Live Activity")
    paket = json.loads(lesen("plugins", "jolt-anzeige", "package.json"))
    pruefe(paket.get("capacitor", {}).get("ios", {}).get("src") == "ios",
           "das Plugin meldet sich bei Capacitor als iOS-Plugin an")
    wurzel = json.loads(lesen("package.json"))
    pruefe("jolt-anzeige" in wurzel.get("dependencies", {}),
           "und steht in den Abhängigkeiten - sonst bindet `cap sync` es nicht ein")
    plugin = lesen("plugins", "jolt-anzeige", "ios", "Sources",
                   "JoltAnzeigePlugin", "JoltAnzeigePlugin.swift")
    pruefe('jsName = "JoltAnzeige"' in plugin
           and "registerPlugin('JoltAnzeige')" in lesen("tools", "ble-huelle-eintrag.js"),
           "der Name im Swift-Plugin ist der, unter dem die Oberfläche es sucht")
    for methode in ("aktualisieren", "beenden", "verfuegbar"):
        pruefe(f'CAPPluginMethod(name: "{methode}"' in plugin
               and f"func {methode}(" in plugin,
               f"{methode} ist deklariert und umgesetzt - eine nur "
               f"deklarierte Methode läuft ins Leere")
    pruefe("JoltAnzeigePlugin" in lesen("plugins", "jolt-anzeige", "Package.swift"),
           "Package.swift kennt das Ziel des Plugins")

    widget = lesen("ios-native", "JoltWidget", "JoltWidget.swift")
    pruefe("supplementalActivityFamilies([.small])" in widget,
           "das Widget meldet die kleine Familie - nur die zeigt CarPlay")
    pruefe("com.apple.widgetkit-extension" in lesen("ios-native", "JoltWidget", "Info.plist"),
           "die Erweiterung ist als WidgetKit-Erweiterung ausgewiesen")
    pruefe("NSSupportsLiveActivities" in lesen("tools", "ios_info_plist.sh"),
           "die App erlaubt Live Activities - ohne den Schlüssel wirft "
           "Activity.request, und es erscheint nichts")
    rb = lesen("tools", "ios_widget.rb")
    pruefe('"#{app_id}.widget"' in rb and 'dst_subfolder_spec = "13"' in rb
           and "add_dependency" in rb,
           "das Skript gibt dem Widget eine eigene Kennung, bettet es in die "
           "App ein und macht es zur Abhängigkeit")
    pruefe("JoltFahrtAttributes.swift" in lesen("tools", "ios_widget.sh"),
           "die gemeinsame Attribute-Datei kommt aus dem Plugin ins Widget - "
           "eine Quelle, zwei Ziele")

    # JS-Modell gegen Swift-Felder.
    fixtur = ("{km_auf_route:100,ist_soc:72.1,soc_gemeldet:true,soc_quelle:'gemessen',"
              "soll_soc:73,rest_km:87.4,reserve_bei_km:160,ankunft_verschiebung_min:12,"
              "naechster_stopp:{name:'X',km_auf_route:141,geplant_soc:19,erwartet_soc:17.6}}")
    skript = ("const vm=require('vm'),fs=require('fs');const w={};"
              "vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),"
              "{window:w,console,Date,JSON,Math,Number,setTimeout,clearTimeout,Promise});"
              f"console.log(JSON.stringify(w.joltAnzeige.modell({fixtur},1)))")
    try:
        ausgabe = subprocess.run(
            ["node", "-e", skript, os.path.join(WURZEL, "frontend", "anzeige.js")],
            capture_output=True, text=True, check=True).stdout
        modell = json.loads(ausgabe)
    except (OSError, subprocess.CalledProcessError, ValueError) as fehler:
        pruefe(False, "das Anzeigemodell lässt sich mit node erzeugen", str(fehler))
        return
    felder = swift_felder(lesen("plugins", "jolt-anzeige", "ios", "Sources",
                                "JoltAnzeigePlugin", "JoltFahrtAttributes.swift"))
    zuordnung = {"JoltAnzeige": modell, "Soc": modell["soc"], "Stopp": modell["stopp"],
                 "Zeile": modell["reserve"]}
    for struktur, vorhanden in zuordnung.items():
        pflicht = [f for f, optional in felder.get(struktur, {}).items() if not optional]
        fehlt = [f for f in pflicht if f not in vorhanden]
        pruefe(struktur in felder and pflicht and not fehlt,
               f"Swift liest {struktur} mit den Pflichtfeldern {pflicht} - "
               f"alle kommen aus anzeige.js", str(fehlt))
    for name in ("ankunft", "rest"):
        pruefe("text" in modell[name], f"{name} trägt den Text, den Swift liest")


def main() -> int:
    teil_app_id()
    teil_symbol()
    teil_skripte()
    teil_signatur()
    teil_workflows()
    teil_live_activity()
    return pruefe.bilanz(
        "Nicht geprüft (nur mit Apple-Konto und Mac-Läufer prüfbar): ob das "
        "Zertifikat zum Profil passt, ob xcodebuild das Projekt signiert und "
        "ob App Store Connect den Upload annimmt. Das zeigt der erste Lauf "
        "von ios-testflight.yml.")


if __name__ == "__main__":
    sys.exit(main())
