# jolt als native iOS-App

Der Umbau des Frontends von einer PWA auf einen nativen SwiftUI-Client.
Dieses Dokument hält fest, **warum** das nötig ist, **was** dabei entsteht und
in welcher Reihenfolge — es ist die Vorlage für die Umsetzung, nicht ihr
Ergebnis.

Der Stand vor dem Umbau trägt den Tag `pwa-stand-2026-09-05`.

---

## Warum überhaupt nativ

Drei Dinge kann eine Web-Oberfläche auf iOS grundsätzlich nicht, und alle drei
sind für jolt keine Nebensache:

**Bluetooth.** Safari implementiert `navigator.bluetooth` auf iOS nicht — aus
Datenschutzgründen, systemweit, seit Jahren und ohne Aussicht auf Änderung.
Deshalb braucht die PWA heute [Bluefy](https://bluefy.app): eine fremde App,
die per CoreBluetooth eine Brücke baut und die Web-API im Seiteninhalt
nachbildet. Das funktioniert, aber es bedeutet, dass die zentrale Funktion von
jolt — den Ladestand aus dem Auto lesen — von einer App abhängt, die uns nicht
gehört und deren Fortbestand niemand zusichert.

**Standort im Hintergrund.** Sobald der Safari-Tab nicht sichtbar ist, pausiert
iOS die Positionsermittlung. Genau deshalb gibt es in `frontend/live.js` den
Bildschirm-Wachhalter mit dem stummen Video — ein Notbehelf gegen ein Problem,
das nativ nicht existiert. Eine Fahrt aufzuzeichnen, während das Telefon in
der Tasche liegt, geht nur mit `CLLocationManager` und
`allowsBackgroundLocationUpdates`.

**CarPlay.** Gibt es für Web-Inhalte nicht, in keiner Form. Apple erlaubt dort
ausschliesslich eigene Vorlagen über eine `CPTemplateApplicationSceneDelegate`.

## Der Weg dorthin führt über Capacitor

Hier stand zuerst, ein WebView-Wrapper löse davon nichts: Er benutze dasselbe
WebKit und habe dieselbe fehlende Bluetooth-API. Der erste Halbsatz stimmt,
der Schluss daraus nicht. Der Zugriff läuft bei Capacitor nicht über die
Web-API, sondern über Plugins, die nativen Code ausführen — es ist ein echtes
Xcode-Projekt, in dem beliebiges Swift liegen darf.

| Grenze | Weg über Capacitor |
|---|---|
| Bluetooth | `@capacitor-community/bluetooth-le` über CoreBluetooth |
| Bildschirm wachhalten | `@capacitor-community/keep-awake`, setzt `isIdleTimerDisabled` |
| Standort im Hintergrund | Plugin über `CLLocationManager` |
| CarPlay | eigener `CPTemplateApplicationSceneDelegate`, echte Swift-Arbeit |

**Den Ausschlag gibt `obd-kern.js`.** Dieses Dokument nennt die Datei weiter
unten das wertvollste Stück des Frontends und Schritt 3 den Prüfstein des
ganzen Umbaus — zu Recht: Bei einer Übersetzung gehen Vorzeichen, Skalierung
und Bytereihenfolge still daneben, und ein falscher Wert sieht plausibel aus.
Genau dieses Risiko entfällt, wenn die Datei weiterläuft statt übersetzt zu
werden. Sie ist 1055 Zeilen lang, und davon fassen **vierzehn** die
Web-Bluetooth-API an, gebündelt in `verbinden`, `verbindungAufbauen`,
`trennen`, `befehl` und `verbindenOhneDialog`. Der Rest ist Rechnerei ohne
Browser-Bezug.

Getauscht wird deshalb nur der Transport: `frontend/obd-ble-nativ.js` bildet
die benutzte Teilmenge von Web Bluetooth nach und beantwortet sie über das
Plugin. Im Kern steht dafür eine einzige neue Funktion, `bt()`, die zur
Laufzeit entscheidet, woher das Bluetooth kommt. Die Zahlen bleiben damit
gleich, weil es dieselbe Rechnung ist.

**Was Capacitor nicht kann.** Sobald iOS die App suspendiert, steht das
JavaScript. Die BLE-Verbindung überlebt und ein Standort-Plugin sammelt
nativ weiter, aber die Ableseschleife für die CAN-Werte läuft nicht mehr.
„Fahrt mit dem Telefon in der Tasche" gibt es damit für GPS, nicht für die
Fahrzeugdaten. Mit Telefon in der Halterung und wachgehaltenem Bildschirm
ist der Fall gegenstandslos — und das ist der Alltag.

**SwiftUI bleibt die Option dahinter, nicht davor.** Wenn die App im Alltag
trägt und CarPlay dazukommen soll, ist der Weg dorthin offen, und er ist
dann besser begehbar als heute: Die übersetzten Byte-Formeln liessen sich
gegen eine laufende native App auf echten Fahrten prüfen statt gegen Bluefy.
Der ehrliche Vorbehalt dazu ist, dass Zwischenlösungen oft dauerhaft werden.

## Was ausdrücklich bleibt

**Das Backend ändert sich nicht.** Die App spricht dieselbe FastAPI, die die
Web-Oberfläche heute schon bedient. Kein neuer Endpunkt ist für den ersten
Schritt nötig.

**Das Web-Frontend bleibt bestehen.** Es ist der Zugang vom Rechner aus, es
funktioniert, und es ist die Rückfallebene, solange die App noch nicht trägt.
Erst wenn sie es tut, ist über einen Rückbau zu reden — vorher nicht.

**`obd-kern.js` wird übersetzt, nicht ersetzt.** Darin stecken siebzehn
Messwerte mit ihren Datenkennungen, die 11-/29-Bit-Adressumschaltung und die
Byte-Formeln, und ein Teil davon ist am Fahrzeug erarbeitet und nicht aus einer
Referenz abgeschrieben. Diese Datei ist das wertvollste Stück des Frontends.

---

## Aufbau

```
ios/
  Jolt.xcodeproj
  Jolt/
    App/            Einstieg, Szenen, Einstellungen
    Netz/           HTTP-Anbindung an jolts API
    OBD/            CoreBluetooth + Protokoll (Übersetzung aus obd-kern.js)
    Fahrt/          Live-Aufzeichnung, Hintergrund-Standort
    Ansichten/      SwiftUI: Planung, Live, Fahrten, Fahrzeuge
    CarPlay/        CPTemplateApplicationSceneDelegate + Statusvorlage
  JoltTests/        Protokolltests gegen bekannte Antworten
```

Deutsche Bezeichner wie im Backend. Das ist keine Marotte, sondern hält die
Begriffe zwischen den Schichten gleich: Was im Backend `verbrauchsfaktor`
heisst, soll in der App nicht `consumptionFactor` heissen.

### Warum ein Verzeichnis im bestehenden Repository

Und nicht ein zweites daneben: Der OBD2-Code existiert dann zweimal — einmal
in JavaScript, einmal in Swift — und muss zusammenbleiben. Wer eine Byte-Formel
korrigiert, muss beide sehen. In getrennten Repositories laufen sie
auseinander, und zwar unbemerkt, weil ein Fehler dort erst am Auto auffällt.

---

## Die vier Bausteine

### 1. Netz — die Anbindung an jolts API

Dünn. Ein `URLSession`-Client, `Codable`-Strukturen für die Antworten, sonst
nichts. Die fachliche Rechnung bleibt im Backend, wo sie steht.

Zugang: `x-token`-Header, wie ihn `deps.aktuelle_sitzung` erwartet. Das Token
kommt aus `POST /api/auth/login` und gehört in die Keychain, nicht in
`UserDefaults`.

Gebraucht werden für den Anfang:

| Zweck | Endpunkt |
|---|---|
| Anmelden | `POST /api/auth/login`, `GET /api/auth/status` |
| Fahrzeuge | `GET /api/fahrzeuge`, `PUT /api/fahrzeuge/{id}` |
| Route rechnen | `POST /api/route` |
| Ladeplan | `POST /api/fahrten/{id}/ladeplan` |
| Fahrtenliste | `GET /api/fahrten`, `GET /api/fahrten/{id}` |
| Fahrt starten | `POST /api/live/start/{fahrt_id}` |
| Aufzeichnung starten | `POST /api/live/aufzeichnung` |
| Messpunkt melden | `POST /api/live/{sitzung_id}/punkt` |
| Zustand lesen | `GET /api/live/{sitzung_id}` |
| Verlauf nachladen | `GET /api/live/{sitzung_id}/punkte` |
| Laufend zusehen | `WebSocket /api/live/{sitzung_id}/ws` |

### 2. OBD — Bluetooth und Protokoll

Der aufwendigste Teil und der, bei dem am meisten schiefgehen kann.

**CoreBluetooth** ersetzt die Bluefy-Brücke: ELM327-Adapter suchen, verbinden,
die serielle Kennung finden, Kommandos schreiben, Antworten in Rahmen
zusammensetzen. Das ist Fleissarbeit mit bekannten Fallstricken
(Mehrrahmen-Antworten, Flusskontrolle).

**Das Protokoll** kommt aus `frontend/obd-kern.js`. Zu übertragen sind:

- die Adressblöcke `BMS`, `KLIMA`, `AKKU11`, `FAHRZEUG`, `DCDC` mit ihren
  `cp`/`sh`/`cra`/`fcsh`-Werten,
- die Liste `MESSWERTE` mit siebzehn Einträgen: Datenkennung, Zieladresse,
  Byte-Formel, `pflicht`-Kennzeichen,
- die Protokollumschaltung: Klima- und 11-Bit-Batteriegerät antworten nur
  unter `ATSP6`, alles andere unter `ATSP7`. Genau daran scheiterten in einer
  Testfahrt **alle** Temperaturwerte, und die Ursache war die Rahmenbreite,
  nicht die Adresse.

**Diese Übersetzung wird geprüft, bevor sie ans Auto kommt.** Vorzeichen,
Skalierung und Bytereihenfolge sind genau die Stellen, an denen eine
Portierung still danebengeht — `soc_roh` ist ein Byte geteilt durch 2,5, der
Batteriestrom `(Rohwert − 150000)/100` über vier Bytes, und beide sähen auch
falsch noch plausibel aus. `JoltTests` bekommt deshalb die aufgezeichneten
Rohantworten aus echten Fahrten als Prüffälle: dieselbe Hex-Antwort hinein,
derselbe Zahlenwert heraus wie in JavaScript.

### 3. Fahrt — Aufzeichnung im Hintergrund

Der Grund, warum das Ganze nativ sein muss.

`CLLocationManager` mit `allowsBackgroundLocationUpdates = true` und der
Berechtigung „Immer erlauben". Dazu `UIBackgroundModes: location` und
`bluetooth-central` in der `Info.plist`.

Zwei Dinge, die die PWA nicht konnte und die hier ohne Umstände gehen:

- **Bildschirm wachhalten** ist `UIApplication.shared.isIdleTimerDisabled =
  true`. Der Video-Hack in `live.js` entfällt ersatzlos.
- **Kein Datenverlust bei Verbindungsabriss.** Messpunkte kommen zuerst in
  eine lokale Warteschlange und gehen von dort ans Backend. Die Fahrt vom 4.9.
  hat 46 % ihrer Dauer in Lücken verbracht, und der grösste Teil davon war
  Funkloch, nicht Sensorausfall — die Punkte gab es, sie kamen nur nie an.

**Zum Melden gibt es zwei Wege, und der zweite ist der bessere:**

`POST /api/live/{sitzung_id}/punkt` braucht die Sitzungs-ID und ein Token.

`POST /api/live/melden` braucht nur den **Logger-Token des Fahrzeugs** (aus
`POST /api/fahrzeuge/{id}/logger-token`). Das Backend sucht die laufende
Sitzung selbst, und wenn keine läuft, antwortet es mit `200` und
`aufgenommen: false` statt mit einem Fehler — ausdrücklich gedacht für ein
Gerät, das unbeaufsichtigt sendet. Genau die Lage, in der ein
Hintergrundprozess ist. Der Token bleibt gültig, während Sitzungs-IDs mit
jeder Fahrt wechseln; ein Hintergrunddienst müsste sonst Zustand pflegen, den
er beim Aufwachen längst verloren hat.

### 4. CarPlay — was geht, und auf welchem Weg

*Quelle: Apples CarPlay Developer Guide, Stand 8.6.2026
(<https://developer.apple.com/download/files/CarPlay-Developer-Guide.pdf>).
Was dort nicht steht, ist unten als Annahme oder offen gekennzeichnet.*

**Das Dashboard selbst lässt sich nicht zeigen.** CarPlay-Apps bestehen aus
einem festen Satz Vorlagen, die iOS zeichnet; Web-Inhalte, eigene Karten und
Diagramme gibt es nicht. Möglich ist eine **native Kurzfassung** — ein Blick auf
das, was man unterwegs wissen muss —, und dafür gibt es zwei Wege, die sich
nicht ausschliessen.

| | Weg A: Widget und Live Activity | Weg B: CarPlay-App mit Vorlagen |
|---|---|---|
| Freigabe von Apple | **keine** („Your app does not need to be a CarPlay app") | Antrag mit Begründung, Prüfung durch Apple |
| Mindestens | iOS 26 | iOS 14 (Driving task) bzw. 16 (EV charging) |
| Wo es erscheint | links vom CarPlay-Dashboard (Widget), im Dashboard oder als Mitteilung (Live Activity) | eigenes Symbol auf dem CarPlay-Startbildschirm |
| Was es kann | ein Blick: wenige Zahlen, aktualisiert von der App | Listen, Informationsseiten, Raster; Auswahl und Schaltflächen |
| Grössen | Widget `systemSmall`, Live Activity `small` (dieselbe wie Apple Watch) | feste Vorlagen, höchstens 2 bis 3 Ebenen tief (Driving task) |
| Swift-Ziel | Widget-Erweiterung (eigenes Ziel im Xcode-Projekt) | Szenen-Delegate in der App selbst |
| Aufwand | mittel | höher, und er beginnt mit dem Warten auf Apple |

**Weg A im Einzelnen.**

- Ein Widget in CarPlay braucht die Familie `.systemSmall`, eine Live Activity
  `.supplementalActivityFamilies([.small])`. Fehlt die kleine Aktivität, zeigt
  CarPlay die kompakten Ansichten der Dynamic Island.
- Ein Widget öffnet die App in CarPlay **nicht**, solange die App keine
  CarPlay-App ist. Es zeigt, es bedient nicht.
- Ein Widget, dessen Daten hinter Datenschutzklasse A oder B liegen, ist in
  CarPlay nutzlos: Das iPhone ist dort meist gesperrt. Die Daten gehören in
  eine ungeschützte Ablage (Klasse C oder keine) — ein Detail, an dem das
  leicht scheitert.
- Die Live Activity ist der bessere Träger für jolt: Sie hat einen Beginn und
  ein Ende — die Fahrt — und wird von der App aktualisiert, die ohnehin läuft
  (Hintergrund-Standort, Dongle).

**Weg B im Einzelnen.**

- Kategorie **Driving task** (`com.apple.developer.carplay-driving-task`) oder
  **EV charging** (`com.apple.developer.carplay-charging`, iOS 16). Eine App
  bekommt eine Kategorie; gewählt wird im Antrag. **Navigation** scheidet aus —
  sie verlangt Abbiegehinweise (`com.apple.developer.carplay-maps`).
- Driving task: Aufgaben, die „wirklich bei der Fahrt helfen"; nur Vorlagen
  (keine eigene Karte); Daten höchstens **alle 10 Sekunden** aktualisieren;
  keine Ortssuche; keine Nutzung ausserhalb des Fahrzeugs.
- EV charging: muss mehr leisten als eine Liste von Ladesäulen; auf einer Karte
  dürfen nur Ladesäulen erscheinen; bis zu fünf Ebenen tief.
- Für alle: Nichts darf zum Griff zum iPhone auffordern, jeder Ablauf muss ohne
  iPhone möglich sein, nichts Unzusammenhängendes (Einstellungen, Konto).
- **Welche Vorlagen es gibt** (Tabelle des Leitfadens, Seite 14, als Bild
  gelesen — im Text sind die Häkchen nicht zu erkennen):

  | Vorlage | Driving task | EV charging |
  |---|---|---|
  | Information (wenige Zeilen, Schaltflächen) | ja | ja |
  | Liste, Raster (bis acht Einträge), Tab-Leiste | ja | ja |
  | Point of interest (Orte) | ja | ja |
  | Alarm, Aktionsblatt | ja | ja |
  | Suche | nein | iOS 27 |
  | Karte | nein | nein (nur Navigation) |
  | Tiefe der Vorlagen | 2, ab iOS 26.4: 3 | 5 |
  | Aktualisierung der Daten | höchstens alle 10 s | keine Grenze genannt |

  Für jolt reicht **in beiden** Kategorien das, was gebraucht wird: eine
  Informationsvorlage („nächster Stopp") und eine Liste der Stopps.
- **Empfehlung: EV charging.** Der Kern von jolt ist die Planung von Ladestopps;
  das ist die Aufgabe der Kategorie. Dazu kommen fünf statt zwei bis drei
  Ebenen (Stoppliste → Stopp → Ausweichstandort), keine genannte
  Aktualisierungsgrenze und die Vorlage für Orte für „nächste Ladesäule". Die
  Bedingung ist, dass die App „mehr leistet als eine Liste von Ladesäulen" und
  auf Karten nur Ladesäulen zeigt — beides trifft zu. **Das Risiko:** Apple
  entscheidet nach dem Antrag, und ein späterer Wechsel der Kategorie hiesse
  vermutlich einen neuen Antrag (Annahme, nicht im Leitfaden belegt). Driving
  task wäre der Rückfall, mit engeren Grenzen.
- Ablauf: Antrag unter developer.apple.com/carplay, Zusatzvereinbarung
  zustimmen, Apple prüft und ordnet dem Entwicklerkonto das Entitlement zu,
  danach neues Provisionierungsprofil mit der CarPlay-Fähigkeit.

*Gestrichen:* Hier stand, die Statusanzeige werde „weniger streng geprüft als
eine Navigationsanzeige". Das steht nirgends im Leitfaden und war eine Annahme.

#### Was angezeigt wird

Dieselben Angaben für beide Wege — alle stehen schon im Zustand, den
`GET /api/live/{sitzung_id}` und der WebSocket liefern:

| Anzeige | Feld im Zustand | Anmerkung |
|---|---|---|
| Ladestand | `ist_soc`, `soc_quelle` | „zuletzt gemessen" kennzeichnen, wenn nicht frisch |
| Reichweite bis Reserve | `reserve_bei_km` | leer, wenn das Ziel ohne Nachladen erreicht wird |
| Nächster Ladestopp | `naechster_stopp` (Name, km, Ankunfts-Ladestand) | nur bei geplanter Fahrt |
| Ankunft | `ankunft_verschiebung_min` | „nach Plan", „+12 min" |
| Rest | `rest_km` | |

Der Verkehr gehört **nicht** dazu: Er steht nur in der Antwort der Planung und
wird nicht gespeichert (TomTom-Bedingungen), also nicht im laufenden Zustand.

Bei einer **Aufzeichnung** ohne Plan bleiben Ladestand und, wenn das Auto
antwortet, Fahrzeugwerte — kein Ladestopp, keine Ankunft. Die Anzeige muss mit
fehlenden Feldern umgehen können; sie zeigt, was da ist, und erfindet nichts.

#### Wie der Zustand zum Swift-Code kommt

Der Zustand liegt im JavaScript der Oberfläche (`live.js: zustandAnzeigen`),
und das läuft, solange die App am Leben gehalten wird — auch bei gesperrtem
Telefon (Hintergrund-Standort, an einer echten Fahrt bestätigt). Ein kleines
Capacitor-Plugin nimmt von dort **ein Anzeigemodell** entgegen und gibt es an
ActivityKit (Weg A) oder an die Vorlage (Weg B). Kein zweiter Weg zum Server,
kein zweiter Token, keine zweite Rechnung.

Das Anzeigemodell ist eine reine Funktion `Zustand → {wenige Zahlen und Texte}`
und lässt sich **ohne Swift und ohne Mac** bauen und prüfen: Rundung, Platzhalter
für fehlende Werte, „veraltet" nach einer Frist, höchstens eine Aktualisierung
alle 10 Sekunden (die Regel von Weg B; Weg A hat eigene Grenzen, siehe unten).

#### Wie Swift-Code in ein erzeugtes Projekt kommt

Das iOS-Projekt wird in der CI **erzeugt** und ist nicht eingecheckt (siehe
„Bauen und Ausliefern") — das soll so bleiben. Der Swift-Code liegt deshalb
eingecheckt neben der Konfiguration und wird nach `cap add ios` ergänzt:

- **Plugin und CarPlay-Szene (Weg B):** als lokales Swift-Paket (wie die
  Community-Plugins), das `cap sync` einbindet. Der Szenen-Delegate steht in
  der `Info.plist` über den Klassennamen mit Modulnamen; `tools/ios_info_plist.sh`
  ergänzt das Szenen-Manifest, das Entitlement geht als Build-Einstellung
  (`CODE_SIGN_ENTITLEMENTS`) hinein. Das berührt das Xcode-Projekt kaum.
  **Annahmen, ungeprüft:** dass sich ein Szenen-Delegate aus einem Swift-Paket
  über seinen Modulnamen in der `Info.plist` einbinden lässt, und dass ein
  CarPlay-Szenen-Manifest auch verlangt, das iPhone-Fenster als Szene zu führen.
  Ob die Capacitor-Vorlage das bereits tut oder umgestellt werden müsste, ist
  offen — und wäre der eigentliche Eingriff in die App.
- **Widget-Erweiterung (Weg A):** ein Widget ist ein **eigenes Ziel** mit eigener
  Bundle-Kennung — als Paket nicht abbildbar. Das Xcode-Projekt muss
  programmatisch ergänzt werden (das Ruby-Werkzeug `xcodeproj` oder XcodeGen auf
  dem macOS-Läufer; `tools/ios_signatur.sh` patcht das Projekt schon heute).
  **Offen:** Die Erweiterung braucht eine eigene App-ID und ein eigenes Profil.
  Ob die automatische Signatur über den API-Schlüssel (`-allowProvisioningUpdates`)
  das für ein zweites Ziel ohne Handarbeit anlegt, ist **nicht geprüft** und
  entscheidet, wie viel von Weg A in der CI läuft. Das zeigt nur ein Versuch.

`tools/check_ios.py` bekommt die Prüfungen, die sich ohne Mac stellen lassen:
Szenen-Manifest vorhanden, Entitlement gesetzt, das Widget-Ziel im Projekt.

#### Was sich ohne Gerät nicht prüfen lässt

Wie in der ganzen App: Die CI beantwortet, ob es **baut**. Ob es im Auto
**erscheint**, zeigt nur CarPlay — auf dem Mac im *CarPlay Simulator* (Teil der
Xcode-Zusatzwerkzeuge) oder im Fahrzeug über TestFlight. Ein Mac fehlt; also
bleibt das Auto der Test, und der erste Versuch dort ist ein Versuch.

Nicht nachgelesen, aber vor dem Bau zu klären: wie lange eine Live Activity
laufen darf und wie oft sie sich aktualisieren lässt (Apple begrenzt beides) —
bei einer Fahrt von acht Stunden ist das keine Nebensache.

---

## Reihenfolge

Jeder Schritt endet mit etwas, das läuft.

Die Schritte 1 bis 6 oben beschreiben den SwiftUI-Weg. Gebaut wird zuerst
die Capacitor-Stufe, weil sie dieselben drei Grenzen nimmt, ohne die
Byte-Formeln anzufassen.

| # | Schritt | Ergebnis | Stand |
|---|---|---|---|
| 1 | Capacitor-Gerüst, Plugin-Hülle, `bt()` im Kern | Bluetooth läuft nativ statt über Bluefy | erledigt |
| 2 | `keep-awake` statt Video-Behelf | Bildschirm bleibt an | erledigt |
| 3 | CI erzeugt und baut das iOS-Projekt | „compiliert es" ohne Mac beantwortbar | gebaut; der erste Lauf scheiterte an der App-ID (Bindestrich), behoben |
| 4 | Apple-Developer-Programm, Signatur, TestFlight | App kommt aufs iPhone | Ablauf fertig (`ios-testflight.yml`), wartet auf Konto und Geheimnisse — Anleitung: [`ios-einrichten.md`](ios-einrichten.md) |
| 5 | Hintergrund-Standort über Plugin | Aufzeichnung bei gesperrtem Bildschirm | eingebaut (`@capacitor-community/background-geolocation`), **ungeprüft auf dem Gerät** |
| 6 | Warteschlange gegen Funklöcher | keine Lücken mehr | erledigt (`live.js`, Stapel-Endpunkt `/punkte`) |
| 7 | Anzeigemodell: Zustand → wenige Zahlen (`live.js`), mit Test | die Grundlage für beide Wege, ohne Swift und ohne Apple | **erledigt:** `frontend/anzeige.js` (reine Funktion, gedrosselter Sender) und `tools/check_anzeige.js`; `live.js` meldet Zustand und Fahrtende, ein Plugin setzt später nur noch das Ziel |
| 8 | CarPlay-Antrag bei Apple (Kategorie wählen, Vorlagen im Leitfaden prüfen) | Entitlement für Weg B | **Entwurf fertig:** [`carplay-antrag.md`](carplay-antrag.md) (Kategorie EV charging, englischer Text zum Einfügen); abgeschickt wird er von dir |
| 9 | Weg A: Plugin, Live Activity, Widget-Ziel in der CI, Signatur | Ladestand und nächster Stopp im CarPlay-Dashboard | **gebaut, ungeprüft auf dem Gerät:** Plugin `plugins/jolt-anzeige` (ActivityKit), Widget-Erweiterung `ios-native/JoltWidget` (Familie `.small`), `tools/ios_widget.sh/.rb` hängt das Ziel nach `cap add ios` ein, `check_ios.py` prüft Modell gegen Swift-Felder. **Offen:** ob die automatische Signatur Kennung und Profil der Erweiterung (`<App-ID>.widget`) beim Export selbst anlegt; ob CarPlay die Activity zeigt. Apple beendet eine Live Activity nach 8 Stunden. Kein `systemSmall`-Widget (nur die Live Activity). Die Erweiterung gilt ab iOS 18 (kleine CarPlay-Familie; `if #available` im `WidgetBundle` baut nicht), die App behält ihre Mindestfassung (sie anzuheben liess `cap sync` ein ungültiges `Package.swift` schreiben). **Ungeprüft:** ob App Store Connect eine Erweiterung mit höherer Mindestfassung als die App annimmt. |
| 10 | Weg B: CarPlay-Szene mit Vorlagen | Liste der Ladestopps im Auto | **gebaut, ungeprüft im Auto:** Entitlement `carplay-charging` ist zugeteilt (Oktober 2026). `JoltCarPlaySceneDelegate` (Liste „Jetzt“ und „Ladestopps“, Stopp-Seite), `JoltAnzeigeStore` als Brücke vom Plugin, `tools/ios_carplay.sh/.rb` (zweite Szene in der Info.plist, AppDelegate, Entitlement). **Offen:** Entitlement im Portal für die App-ID einschalten und `CARPLAY_ENTITLEMENT` setzen ([`ios-einrichten.md`](ios-einrichten.md)); ob die Szene beim Kaltstart ohne Oberfläche etwas zeigt (dann nur der abgelegte letzte Stand); **Aufzeichnung starten und beenden** geht aus der Liste (Start mit dem zuletzt benutzten Fahrzeug, Beenden mit Rückfrage) — aber nur, wenn die Oberfläche läuft; beim Kaltstart ohne sie sagt CarPlay, dass jolt auf dem iPhone geöffnet werden muss. Ein Start mit gesperrtem iPhone scheitert womöglich am Standort (die Oberfläche fragt ihn beim Start ab); der Grund erscheint dann in CarPlay. Eine Fahrzeugwahl in CarPlay fehlt noch. |

**Schritt 4 ist der Engpass, nicht der Code.** Alles bis einschliesslich 3
läuft ohne Apple-Konto und ohne Mac. Ab 4 geht nichts mehr ohne das
Developer-Programm: Ohne Signatur gibt es keinen Weg auf ein Gerät, und ohne
Mac oder hinterlegte Zertifikate keinen signierten Bau.

CarPlay steht am Ende, aber **nicht mehr hinter SwiftUI**: Beide Wege laufen
über Capacitor mit einem kleinen Plugin, die App bleibt, wie sie ist. Der
Antrag (8) und das Anzeigemodell (7) hängen an nichts und können sofort
beginnen; 9 und 10 brauchen ein Gerät mit iOS 26 bzw. ein Fahrzeug.

**Empfohlene Reihenfolge:** 7 und 8 gleichzeitig, danach 9. Weg A bringt ohne
Antrag und ohne Warten etwas ins Auto; Weg B ist der Mehrwert für die Liste der
Stopps und lohnt, wenn sie unterwegs gebraucht wird.

---

## Bauen und Ausliefern

**Es gibt kein Xcode und keinen Mac.** Das ist die Bedingung, unter der
alles hier steht, und der Capacitor-Weg kommt damit zurecht: Das
iOS-Projekt wird nicht von Hand gepflegt, sondern bei jedem Lauf aus
`package.json` und `capacitor.config.json` erzeugt. Deshalb ist `ios/` auch
nicht eingecheckt — es wäre eine zweite Wahrheit neben der Konfiguration,
und die beiden liefen unbemerkt auseinander.

**Automatisch bauen** über GitHub Actions: `.github/workflows/ios.yml` legt
das Projekt mit `cap add ios` an, ergänzt die `Info.plist` über
`tools/ios_info_plist.sh` und baut gegen den Simulator, ohne Signatur.

Der Pfadfilter ist eng: `frontend/**` steht **nicht** darin. Die App lädt
ihre Oberfläche zur Laufzeit vom Server (`server.url` in
`capacitor.config.json`), eine geänderte Zeile in `live.js` braucht also
keinen neuen App-Bau, sondern geht den gewohnten Weg über den Container.
Genau dafür ist der Aufbau so gewählt — der schnelle Deploy-Weg bleibt.

Die JavaScript-seitigen Prüfungen laufen dagegen bei jedem Commit in
`ci.yml`, auf einem Linux-Läufer und damit ohne Minutenfaktor:
`tools/check_ble_bruecke.js` spielt den Weg einer Runde am Auto gegen einen
nachgebildeten Dongle durch, und ein Vergleich stellt sicher, dass
`frontend/ble-plugin.js` noch zu seinem Eintrag passt.

Läuft auf `macos-latest`, und dafür gilt bei privaten Repositories ein
**Minutenfaktor von 10** — eine Minute auf einem Mac-Läufer zählt wie zehn
gegen das Kontingent. Deshalb baut der Ablauf nur gegen den Simulator und
signiert nicht: Ein Simulatorbau braucht weder Zertifikat noch
Bereitstellungsprofil und ist der billigste Weg, „compiliert überhaupt noch"
zu beantworten.

**Auf Geräte kommt die App über TestFlight**, und ohne Mac führt daran kein
Weg vorbei: Der übliche Ersatz — Gerät ans Kabel, Xcode vertraut ihm, App
läuft sieben Tage — setzt genau das Xcode voraus, das hier fehlt.

**Ein Apple-Developer-Programm für 99 $/Jahr ist damit Pflicht**, und zwar
früher als auf dem SwiftUI-Weg. Was danach zu tun ist, lässt sich
vollständig ohne Mac erledigen; die Schritte stehen mit Befehlen in
[`ios-einrichten.md`](ios-einrichten.md):

1. Distributionszertifikat — die Signieranfrage (CSR) erzeugt `openssl`.
2. App-ID `de.thesmarthome.jolt` (ohne Bindestrich: Capacitor lehnt ihn ab),
   die App in App Store Connect und ein API-Schlüssel für den Upload.
3. API-Schlüssel (Rolle Admin) und Team-ID als vier Repository-Geheimnisse;
   Zertifikat und Profil legt Apple selbst an. Wer manuell signieren will,
   ergänzt drei weitere.

Der Ablauf dahinter ist fertig: `.github/workflows/ios-testflight.yml`
archiviert (automatisch signiert, manuell als Rückfall) und lädt direkt nach TestFlight hoch,
ausgelöst von Hand oder per Tag `ios-*`. `tools/ios_signatur.sh` stellt dafür
nur das App-Ziel um (eine Kommandozeilen-Einstellung träfe auch die
Swift-Pakete, die keine Profile kennen), `tools/ios_symbol.sh` setzt das
jolt-Symbol statt des Capacitor-Logos, `tools/check_ios.py` prüft in der
normalen CI alles, was sich ohne Mac prüfen lässt.

Erst danach ist die App auf dem Telefon. Bis dahin beantwortet die CI nur,
ob sie sich bauen lässt — was nicht wenig ist, aber eben noch nichts fährt.

---

## Was dieses Dokument nicht klärt

- **Wie viel der Web-Oberfläche später verschwindet.** Sinnvoll erst zu
  entscheiden, wenn die App im Alltag trägt.
- **Ob das iPad als Testgerät taugt.** Nur die Mobilfunk-Ausführungen haben
  einen echten GPS-Empfänger; reine WLAN-Modelle schätzen die Position und sind
  für eine Fahrtaufzeichnung unbrauchbar.
- **Ob Apple das CarPlay-Entitlement erteilt**, und für welche Kategorie
  (Driving task oder EV charging; eine App bekommt eine). Davon hängt nur Weg B
  ab, Weg A nicht.
- ~~Ob das Fahrzeug CarPlay hat und welche iOS-Version das iPhone hat.~~
  **Beantwortet (5.10.2026): kabelloses CarPlay, neueste iOS-Version.** Weg A ist
  damit möglich. Kabellos heisst, dass das iPhone in der Tasche bleibt und
  gesperrt ist — genau der Fall, in dem Widget-Daten hinter Datenschutzklasse A
  oder B nichts zeigen. Daten und Anzeige gehören deshalb in eine ungeschützte
  Ablage.
- **Ob die Capacitor-Vorlage das iPhone-Fenster schon als Szene führt**, wie es
  ein CarPlay-Szenen-Manifest voraussetzt (Weg B).
- **Ob die automatische Signatur über den API-Schlüssel eine zweite App-ID für
  die Widget-Erweiterung selbst anlegt.** Nicht geprüft; ein Versuch in der CI
  klärt es.
- **Wie lange eine Live Activity bei einer langen Fahrt läuft.** Nicht
  nachgelesen.
