#!/usr/bin/env python3
"""Checks the iOS delivery as far as it can be checked without a Mac.

The signed build only runs on a macOS runner and with an Apple account. But
whatever can be noticed beforehand should be noticed beforehand - the first
iOS run on `main` failed because of an app ID with a hyphen, which Capacitor
rejects, and a look at this file would have caught it. It costs nothing,
and a macOS run costs ten times as many minutes.

Without network, without database, without Apple:

    ./tools/check_ios.py
"""
import json
import os
import re
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
verify = Check()


def load(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as file:
        return file.read()


def part_app_id() -> None:
    verify.section("App-ID")
    config = json.loads(load("capacitor.config.json"))
    app_id = config.get("appId", "")
    # The rule from @capacitor/cli (validateAppId): Java package form, no
    # hyphen, every segment starts with a letter. Re-implemented because
    # `cap add ios` only checks this on the Mac runner.
    segmente = app_id.split(".")
    valid = (len(segmente) >= 2
               and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", s)
                       for s in segmente))
    verify(valid,
           "die App-ID erfüllt Capacitors Regeln - sonst bricht `cap add ios` "
           "ab, ehe überhaupt etwas gebaut wird", app_id)
    docs = load("ios-einrichten.md")
    verify(app_id in docs,
           "und die Einrichtungsanleitung nennt genau diese ID", app_id)
    old = [d for d in ("konzept-ios-app.md", "ios-einrichten.md")
            if "the-smarthome.jolt" in load(d)]
    verify(not old,
           "keine Anleitung nennt mehr eine ID, die der Konfiguration "
           "widerspricht", str(old))


def part_symbol() -> None:
    verify.section("App-Symbol")
    with open(os.path.join(ROOT, "tools", "ios-app-icon.png"), "rb") as f:
        header = f.read(33)
    extent, elevation, depth, color_type = struct.unpack(">IIBB", header[16:26])
    verify(header[:8] == b"\x89PNG\r\n\x1a\n", "das App-Symbol ist eine PNG-Datei")
    verify((extent, elevation) == (1024, 1024),
           "und genau 1024 × 1024 Pixel gross - mehr verlangt App Store "
           "Connect, weniger lehnt es ab", f"{extent}×{elevation}")
    # Colour type 2 = RGB, 3 = palette; 4 and 6 have an alpha channel. Apple
    # rejects such icons on upload ("Invalid large app icon").
    verify(color_type in (0, 2, 3) and depth == 8,
           "ohne Alphakanal - Transparenz im großen Symbol lehnt Apple ab",
           f"Farbtyp {color_type}")


def part_scripts() -> None:
    verify.section("Skripte")
    for name in ("ios_info_plist.sh", "ios_symbol.sh", "ios_signatur.sh",
                 "ios_widget.sh", "ios_carplay.sh"):
        result = subprocess.run(
            ["bash", "-n", os.path.join(ROOT, "tools", name)],
            capture_output=True, text=True)
        verify(result.returncode == 0,
               f"{name} hat keinen Syntaxfehler", result.stderr.strip())

    plist = load("tools", "ios_info_plist.sh")
    for keyname in ("NSBluetoothAlwaysUsageDescription",
                       "NSLocationWhenInUseUsageDescription",
                       "ITSAppUsesNonExemptEncryption"):
        verify(keyname in plist,
               f"die Info.plist bekommt {keyname} - ohne ihn räumt iOS "
               f"die App ab oder TestFlight hält jeden Bau an")
    verify("bluetooth-central" in plist,
           "und den Hintergrundmodus für die Dongle-Verbindung")


def part_signature() -> None:
    """Run ios_signatur.sh against a dummy of the Capacitor template.

    The scripts are written portably (sed with a backup file), so this
    also runs here. Mainly the refusal is checked: a script that carries
    on silently when the template has changed signs wrongly.
    """
    verify.section("Signatur-Skript")
    import tempfile
    script = os.path.join(ROOT, "tools", "ios_signatur.sh")
    template = ("\t\tbuildSettings = {\n\t\t\tCODE_SIGN_STYLE = Automatic;\n\t\t};\n"
               "\t\tbuildSettings = {\n\t\t\tCODE_SIGN_STYLE = Automatic;\n\t\t};\n")

    def cycle(contents: str, team: str = "ABCDE12345", profile: str = "jolt AppStore"):
        with tempfile.TemporaryDirectory() as folder:
            fs_path = os.path.join(folder, "project.pbxproj")
            with open(fs_path, "w", encoding="utf-8") as f:
                f.write(contents)
            r = subprocess.run(["bash", script, fs_path, team, profile],
                               capture_output=True, text=True)
            with open(fs_path, encoding="utf-8") as f:
                return r.returncode, f.read(), r.stderr

    rc, text, _ = cycle(template)
    verify(rc == 0 and text.count("CODE_SIGN_STYLE = Manual;") == 2
           and "Automatic" not in text,
           "beide Konfigurationen des App-Ziels werden auf Manuell gestellt")
    verify(text.count('PROVISIONING_PROFILE_SPECIFIER = "jolt AppStore";') == 2
           and text.count("DEVELOPMENT_TEAM = ABCDE12345;") == 2,
           "mit Team und Profilname")
    rc, text, _ = cycle(template + "\t\t\tCODE_SIGN_STYLE = Automatic;\n")
    verify(rc != 0 and "Manual" not in text,
           "ändert sich die Vorlage (drei statt zwei Stellen), bricht das "
           "Skript ab, statt irgendwo falsch zu signieren")
    rc, _, _ = cycle(template, team="ab; rm -rf")
    verify(rc != 0, "eine Team-ID mit Sonderzeichen wird abgelehnt")
    rc, _, _ = cycle(template, profile='x" ; evil')
    verify(rc != 0, "ein Profilname mit Anführungszeichen ebenso")


def part_workflows() -> None:
    verify.section("Workflows")
    ios = load(".github", "workflows", "ios.yml")
    fliegen = load(".github", "workflows", "ios-testflight.yml")

    # Capacitor 8 uses the Swift Package Manager: there is no
    # .xcworkspace, and a `-workspace` is a build that never starts.
    for name, text in (("ios.yml", ios), ("ios-testflight.yml", fliegen)):
        without_comments = "\n".join(
            z for z in text.splitlines() if not z.lstrip().startswith("#"))
        verify("-workspace" not in without_comments
               and "pod " not in without_comments,
               f"{name} baut ohne CocoaPods-Workspace - Capacitor 8 nimmt "
               f"Swift-Pakete")
    verify("-project ios/App/App.xcodeproj" in ios
           and "-project ios/App/App.xcodeproj" in fliegen,
           "beide Abläufe bauen das Projekt, das `cap add ios` anlegt")

    # The simulator build must not sign, the archive build must.
    verify("CODE_SIGNING_ALLOWED=NO" in ios,
           "der Simulatorbau läuft unsigniert - er braucht kein Konto")
    verify("archive" in fliegen and "-exportArchive" in fliegen
           and "destination</key><string>upload" in fliegen,
           "der TestFlight-Ablauf archiviert, exportiert und lädt hoch")

    # Both flows must produce the same project, otherwise one builds
    # something other than what the other delivers.
    for step in ("cap add ios", "tools/ios_info_plist.sh",
                    "tools/ios_symbol.sh", "tools/ios_widget.sh",
                    "cap sync ios"):
        verify(step in ios and step in fliegen,
               f"beide Abläufe führen `{step}` aus")

    # Every referenced secret must be in the instructions - otherwise the
    # error message contains a name that nobody explains.
    docs = load("ios-einrichten.md")
    secrets = sorted(set(re.findall(r"secrets\.([A-Z0-9_]+)", fliegen)))
    verify(len(secrets) == 7,
           "der Ablauf kennt sieben Geheimnisse: vier für den automatischen "
           "Weg, drei weitere für den manuellen", str(secrets))
    verify("MODUS=automatisch" in fliegen and "MODUS=manuell" in fliegen
           and "-allowProvisioningUpdates" in fliegen,
           "und wählt danach: ohne Zertifikat und Profil signiert Apple "
           "selbst, mit beiden die mitgelieferten")
    verify("CODE_SIGNING_ALLOWED=NO" in fliegen,
           "im automatischen Modus wird unsigniert archiviert - beim "
           "Archivieren verlangt Xcode sonst ein Entwicklungsprofil und damit "
           "ein registriertes Gerät (der erste echte Lauf scheiterte daran)")
    verify("EXTRA[@]+" in fliegen,
           "leere Arrays werden mit set -u auf dem alten bash des Mac-Läufers "
           "sicher aufgelöst - sonst bricht der Export im manuellen Modus ab")
    lacking = [g for g in secrets if f"`{g}`" not in docs]
    verify(not lacking,
           "und die Anleitung erklärt jedes davon", str(lacking))

    verify(re.search(r"^on:\s*\n\s+workflow_dispatch:", fliegen, re.M)
           and "pull_request" not in fliegen,
           "der Upload läuft nur von Hand oder per Tag - nie auf Pull "
           "Requests, wo Geheimnisse nicht zur Verfügung stehen und ein "
           "Mac-Lauf zehnfach zählt")
    verify("frontend/**" not in ios,
           "frontend/ löst keinen App-Bau aus - die Oberfläche kommt zur "
           "Laufzeit vom Server")
    verify("if: always()" in fliegen and "delete-keychain" in fliegen,
           "der flüchtige Schlüsselbund wird auch nach einem Fehler gelöscht")


def swift_fields(text: str) -> dict:
    """The fields of each `struct` in a Swift file: name -> {field: optional?}.

    Sufficient for the plain value types of this file; it is not a parser
    for Swift."""
    result = {}
    for hit in re.finditer(r"struct (\w+)[^{]*\{", text):
        depth, i = 1, hit.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        body = text[hit.end():i]
        # Nested structures do not belong to the fields of the outer one.
        flat = re.sub(r"struct \w+[^{]*\{[^{}]*\}", "", body)
        result[hit.group(1)] = {
            m.group(1): m.group(2).endswith("?")
            for m in re.finditer(r"public var (\w+): ([\w.\[\]]+\??)", flat)}
    return result


def part_live_activity() -> None:
    """Plugin, widget and display model must fit together.

    Whether the Live Activity appears in CarPlay is shown only by a device.
    What can be checked without a Mac: that the Swift side gets from the
    JavaScript side every required field that it reads - if one is missing,
    reading the model fails and the display stays empty without anything
    crashing.
    """
    verify.section("Live Activity")
    bundle_ = json.loads(load("plugins", "jolt-anzeige", "package.json"))
    verify(bundle_.get("capacitor", {}).get("ios", {}).get("src") == "ios",
           "das Plugin meldet sich bei Capacitor als iOS-Plugin an")
    root = json.loads(load("package.json"))
    verify("jolt-anzeige" in root.get("dependencies", {}),
           "und steht in den Abhängigkeiten - sonst bindet `cap sync` es nicht ein")
    ignoriert = subprocess.run(["git", "check-ignore", "-q",
        "plugins/jolt-anzeige/ios/Sources/JoltAnzeigePlugin/JoltAnzeigePlugin.swift"],
        cwd=ROOT).returncode == 0
    verify(not ignoriert,
           "die Plugin-Quellen werden von .gitignore nicht verschluckt - `ios/` "
           "traf auch plugins/*/ios, und die CI fand die Dateien nicht")
    plugin = load("plugins", "jolt-anzeige", "ios", "Sources",
                   "JoltAnzeigePlugin", "JoltAnzeigePlugin.swift")
    verify('jsName = "JoltAnzeige"' in plugin
           and "registerPlugin('JoltAnzeige')" in load("tools", "ble-shell-entry.js"),
           "der Name im Swift-Plugin ist der, unter dem die Oberfläche es sucht")
    for method in ("aktualisieren", "beenden", "verfuegbar"):
        verify(f'CAPPluginMethod(name: "{method}"' in plugin
               and f"func {method}(" in plugin,
               f"{method} ist deklariert und umgesetzt - eine nur "
               f"deklarierte Methode läuft ins Leere")
    verify("JoltAnzeigePlugin" in load("plugins", "jolt-anzeige", "Package.swift"),
           "Package.swift kennt das Ziel des Plugins")

    widget = load("ios-native", "JoltWidget", "JoltWidget.swift")
    verify("supplementalActivityFamilies([.small])" in widget,
           "das Widget meldet die kleine Familie - nur die zeigt CarPlay")
    verify("com.apple.widgetkit-extension" in load("ios-native", "JoltWidget", "Info.plist"),
           "die Erweiterung ist als WidgetKit-Erweiterung ausgewiesen")
    verify("NSSupportsLiveActivities" in load("tools", "ios_info_plist.sh"),
           "die App erlaubt Live Activities - ohne den Schlüssel wirft "
           "Activity.request, und es erscheint nichts")
    rb = load("tools", "ios_widget.rb")
    verify('"#{app_id}.widget"' in rb and 'dst_subfolder_spec = "13"' in rb
           and "add_dependency" in rb,
           "das Skript gibt dem Widget eine eigene Kennung, bettet es in die "
           "App ein und macht es zur Abhängigkeit")
    verify("JoltFahrtAttributes.swift" in load("tools", "ios_widget.sh"),
           "die gemeinsame Attribute-Datei kommt aus dem Plugin ins Widget - "
           "eine Quelle, zwei Ziele")

    # JS model against Swift fields.
    fixture = ("{km_on_route:100,actual_soc:72.1,soc_reported:true,soc_quelle:'gemessen',"
              "plan_soc:73,remaining_km:87.4,reserve_at_km:160,arrival_shift_min:12,"
              "next_stop:{name:'X',km_on_route:141,planned_soc:19,expected_soc:17.6}}")
    # A trip that began seventy minutes ago, so that history, bars and
    # regeneration all carry fields, plus measured values for the auxiliary
    # loads.
    extras = ("{plan:{stops:[{name:'Ionity',km_on_route:141,arrival_soc:19,"
              "departure_soc:80,charge_time_minutes:25,operator:'Ionity',max_kw:350}]},"
              "track:(()=>{const s=[];let km=0,n=0,e=0,g=0;"
              "for(let t=-4200000;t<=0;t+=12000){s.push({timestamp:1e12+t,gps:km,net:n,disch:e,chg:g});"
              "km+=0.233;n+=0.035;e+=0.042;g+=0.007}return s})(),"
              "vals:{aux_load_kw:{val:1.8,timestamp:1e12},ptc_current_a:{val:5,timestamp:1e12},"
              "voltage_v:{val:380,timestamp:1e12},compressor_w:{val:450,timestamp:1e12},"
              "batterie_c:{val:27,timestamp:1e12}}}")
    # What is sent is not the pure model but `withImages`: `look` and
    # `tileImages` are added to it. With style "a" and a dummy of the canvas.
    script = ("const vm=require('vm'),fs=require('fs'),p=require('path');"
              "const w={localStorage:{getItem:()=>'a',setItem(){}}};w.window=w;"
              "w.document={createElement:()=>({getContext:()=>new Proxy({},{get:(z,n)=>"
              "n==='measureText'?()=>({width:10}):n==='createLinearGradient'?()=>({addColorStop(){}}):()=>{},"
              "set:()=>true}),toDataURL:()=>'data:image/png;base64,QUJD'})};"
              "const k={window:w,document:w.document,console,Date,JSON,Math,Number,setTimeout,clearTimeout,Promise};"
              "vm.createContext(k);"
              "for(const f of ['tiles.js','display.js'])"
              "vm.runInContext(fs.readFileSync(p.join(process.argv[1],f),'utf8'),k);"
              f"const m=w.joltDisplay.model({fixture},1e12,{extras});"
              "console.log(JSON.stringify({modell:w.joltDisplay.toNative(m),gesendet:w.joltDisplay.toNative(w.joltDisplay.withImages(m,1e12))}))")
    try:
        output = subprocess.run(
            ["node", "-e", script, os.path.join(ROOT, "frontend")],
            capture_output=True, text=True, check=True).stdout
        raw = json.loads(output)
        model, sent = raw["modell"], raw["gesendet"]
    except (OSError, subprocess.CalledProcessError, ValueError) as failure:
        verify(False, "das Anzeigemodell lässt sich mit node erzeugen", str(failure))
        return
    fields = swift_fields(load("plugins", "jolt-anzeige", "ios", "Sources",
                                "JoltAnzeigePlugin", "JoltFahrtAttributes.swift"))
    assignment = {"JoltAnzeige": sent, "Soc": model["soc"], "Stopp": model["stopp"],
                 "Zeile": model["reserve"], "Verlauf": model["verlauf"],
                 "Fenster": model["verlauf"]["fenster"][0],
                 "Rekup": model["verlauf"]["rekup"], "Neben": model["neben"],
                 "Listenstopp": (model["stoppListe"] or [{}])[0]}
    for structure, present in assignment.items():
        # The template is fully populated, so every field that Swift knows must
        # be in the model - a typo in the name (kwh100 versus kwh_100) otherwise
        # turns a required field into a read error and an optional one into a
        # display that never shows anything.
        every = list(fields.get(structure, {}))
        missing = [f for f in every if f not in present]
        verify(structure in fields and every and not missing,
               f"Swift liest {structure} ({', '.join(every)}) - jedes Feld "
               f"kommt aus display.js", str(missing))
    for name in ("ankunft", "rest"):
        verify("text" in model[name], f"{name} trägt den Text, den Swift liest")

    # The tile images: seven slots, and the Swift side uses the same names.
    # A typo here would mean: the images arrive and are never displayed, without
    # any error message.
    place = ["soc", "ankunft", "reserve", "verbrauch", "neben", "rekup", "stopps"]
    verify(sorted(sent.get("kachelBilder", {})) == sorted(place),
           "die Oberfläche liefert Bilder für genau die sieben Kachelplätze")
    scene = load("plugins", "jolt-anzeige", "ios", "Sources", "JoltAnzeigePlugin",
                  "JoltCarPlaySceneDelegate.swift")
    verify(all(f'bild("{n}")' in scene for n in place),
           "und die CarPlay-Szene fragt jeden dieser Plätze ab",
           str([n for n in place if f'bild("{n}")' not in scene]))
    plugin = load("plugins", "jolt-anzeige", "ios", "Sources", "JoltAnzeigePlugin",
                   "JoltAnzeigePlugin.swift")
    verify("fuerActivity.kachelBilder = nil" in plugin,
           "die Live Activity bekommt die Bilder nicht - sie darf höchstens 4 KB tragen")
    verify("CPGridTemplate.maximumGridButtonImageSize" in scene,
           "Bilder werden auf die Grösse begrenzt, die CarPlay für Kacheln zulässt")


def part_carplay() -> None:
    """The CarPlay scene: names, templates, switches.

    Whether CarPlay shows it is only decided by a car (or the CarPlay
    Simulator on the Mac). Here is what could be wrong beforehand: a class
    name in the Info.plist that is called differently in Swift - iOS would
    then find no scene and report nothing -, a template that the category
    does not allow (Apple then rejects the app during review), and an
    entitlement that breaks the signed build as long as it is missing in
    the portal.
    """
    verify.section("CarPlay-Szene")
    delegate = load("plugins", "jolt-anzeige", "ios", "Sources",
                     "JoltAnzeigePlugin", "JoltCarPlaySceneDelegate.swift")
    sh = load("tools", "ios_carplay.sh")
    rb = load("tools", "ios_carplay.rb")
    name = re.search(r"@objc\((\w+)\)", delegate)
    verify(name and name.group(1) in sh,
           "die Info.plist nennt die Klasse, die Swift unter diesem Namen anmeldet",
           name.group(1) if name else "kein @objc(...)")
    verify("CPTemplateApplicationSceneDelegate" in delegate
           and "didConnect interfaceController" in delegate
           and "setRootTemplate" in delegate,
           "die Klasse ist ein CarPlay-Szenen-Delegate und setzt eine Wurzelvorlage")
    verify("CPTemplateApplicationSceneSessionRoleApplication" in sh
           and "CPTemplateApplicationSceneSessionRoleApplication" in rb,
           "Info.plist und AppDelegate sprechen von derselben Szenenrolle")
    verify("UIApplicationSupportsMultipleScenes true" in sh,
           "mehrere Szenen sind an - ohne das bekommt CarPlay keine eigene")
    allowed = ("CPListTemplate", "CPInformationTemplate", "CPGridTemplate",
               "CPTabBarTemplate", "CPPointOfInterestTemplate", "CPAlertTemplate",
               "CPActionSheetTemplate")
    used = set(re.findall(r"\bCP\w*Template\b", delegate))
    forbidden = sorted(t for t in used
                       if t not in allowed and t != "CPTemplateApplicationScene"
                       and t != "CPTemplate")
    verify(not forbidden,
           "nur Vorlagen, die die Kategorie EV charging erlaubt (keine Karte, "
           "keine Suche, keine Navigation)", str(forbidden))
    verify("CPMapTemplate" not in delegate and "CPNavigationSession" not in delegate,
           "ausdruecklich keine Kartenvorlage und keine Navigationssitzung")
    plugin = load("plugins", "jolt-anzeige", "ios", "Sources",
                   "JoltAnzeigePlugin", "JoltAnzeigePlugin.swift")
    verify("JoltAnzeigeStore.shared.setzen(zustand)" in plugin
           and "JoltAnzeigeStore.shared.setzen(nil)" in plugin
           and "JoltAnzeigeStore.shared.beobachten" in delegate,
           "das Plugin fuettert den Speicher, die Szene liest und beobachtet ihn")
    verify("stoppListe = nil" in plugin,
           "die Live Activity bekommt die Stoppliste nicht (4-KB-Grenze)")
    verify("com.apple.developer.carplay-charging" in rb,
           "der Entitlement der Kategorie EV charging")
    verify("CODE_SIGN_ENTITLEMENTS" in rb and 'entitlement = (ARGV[1] || "false") == "true"' in rb,
           "und er wird nur auf Verlangen eingetragen")

    ios = load(".github", "workflows", "ios.yml")
    fliegen = load(".github", "workflows", "ios-testflight.yml")
    verify("tools/ios_carplay.sh ios/App true" in ios,
           "der Simulatorbau prueft den ganzen Weg, mit Entitlement")
    verify("tools/ios_carplay.sh" in fliegen and "inputs.carplay" in fliegen
           and "CARPLAY_ENTITLEMENT" in fliegen,
           "der signierte Bau schaltet ihn ueber ein Häkchen oder eine "
           "Repository-Variable - bis er im Portal eingeschaltet ist, bleibt er aus")
    verify(re.search(r"carplay:\s*\n\s+description:.*?default: false", fliegen, re.S) is not None,
           "und das Häkchen steht auf aus")
    # Start and stop from CarPlay: plugin, scene and UI must speak the same
    # names, otherwise a button in the car does nothing.
    trips = load("frontend", "trips.js")
    for method in ("bereit", "aktionErgebnis"):
        verify(f'CAPPluginMethod(name: "{method}"' in plugin
               and f"func {method}(" in plugin and f"native.{method}(" in trips,
               f"{method}: im Plugin deklariert und umgesetzt, in trips.js aufgerufen")
    verify('notifyListeners("carplayAktion"' in plugin and '"carplayAktion"' in trips,
           "das Ereignis heisst auf beiden Seiten carplayAktion")
    verify('aktionAnfordern("starten")' in delegate and 'aktionAnfordern("beenden")' in delegate
           and 'action === "starten"' in trips and 'action === "beenden"' in trips,
           "Starten und Beenden: dieselben Aktionsnamen in Szene und Oberflaeche")
    verify("CPAlertTemplate" in delegate and "Aufzeichnung beenden?" in delegate,
           "Beenden fragt vorher nach - ein Tippen aus Versehen schliesst keine Fahrt ab")
    verify("brueckeMelden(true)" in plugin and "guard moeglich" in load(
               "plugins", "jolt-anzeige", "ios", "Sources", "JoltAnzeigePlugin",
               "JoltAnzeigeStore.swift"),
           "ohne geladene Oberflaeche (Kaltstart) wird keine Aktion versprochen - CarPlay sagt es")
    verify("asyncAfter" in delegate and "Keine Antwort von jolt" in delegate,
           "antwortet die Oberflaeche nicht, bleibt 'wird gestartet' nicht ewig stehen")

    docs = load("ios-einrichten.md")
    verify("CarPlay EV Charging" in docs and "CARPLAY_ENTITLEMENT" in docs,
           "die Anleitung erklaert, wie er im Portal eingeschaltet wird")


def main() -> int:
    part_app_id()
    part_symbol()
    part_scripts()
    part_signature()
    part_workflows()
    part_live_activity()
    part_carplay()
    return verify.balance(
        "Nicht geprüft (nur mit Apple-Konto und Mac-Läufer prüfbar): ob das "
        "Zertifikat zum Profil passt, ob xcodebuild das Projekt signiert und "
        "ob App Store Connect den Upload annimmt. Das zeigt der erste Lauf "
        "von ios-testflight.yml.")


if __name__ == "__main__":
    sys.exit(main())
