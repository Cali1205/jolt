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

### 4. CarPlay — Statusanzeige

Klein halten. `CPInformationTemplate` oder `CPListTemplate` mit Ladestand,
nächstem Ladestopp und Ankunftszeit — dieselben Daten, die
`GET /api/live/{sitzung_id}` ohnehin liefert. Keine Karte, keine
Abbiegehinweise.

**Der Berechtigungsantrag ist der lange Posten.** CarPlay-Entitlements
vergibt Apple auf Antrag im Developer-Portal, mit Begründung des
Anwendungsfalls, und das dauert Wochen. Der Antrag hängt an keiner Zeile Code
und sollte deshalb **als Erstes** gestellt werden, parallel zu allem anderen.
Die einfache Statusanzeige wird dabei weniger streng geprüft als eine
Navigationsanzeige — ein weiterer Grund, klein anzufangen.

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
| 3 | CI erzeugt und baut das iOS-Projekt | „compiliert es" ohne Mac beantwortbar | erledigt |
| 4 | Apple-Developer-Programm, Signatur, TestFlight | App kommt aufs iPhone | offen, siehe unten |
| 5 | Hintergrund-Standort über Plugin | Aufzeichnung bei gesperrtem Bildschirm | offen |
| 6 | Warteschlange gegen Funklöcher | keine Lücken mehr | offen |
| 7 | SwiftUI, falls CarPlay dazukommt | siehe oben | zurückgestellt |

**Schritt 4 ist der Engpass, nicht der Code.** Alles bis einschliesslich 3
läuft ohne Apple-Konto und ohne Mac. Ab 4 geht nichts mehr ohne das
Developer-Programm: Ohne Signatur gibt es keinen Weg auf ein Gerät, und ohne
Mac oder hinterlegte Zertifikate keinen signierten Bau.

CarPlay ist bewusst ans Ende gerückt — es war der einzige Punkt, der
zwingend nach SwiftUI führt, und es wird vorerst nicht gebraucht.

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
vollständig ohne Mac erledigen, aber es ist Handarbeit beim ersten Mal:

1. Im Developer-Portal ein Distributionszertifikat anlegen. Die dafür nötige
   Signieranfrage (CSR) erzeugt `openssl` auf jedem Rechner, ein Mac ist
   dafür nicht nötig.
2. In App Store Connect eine App-ID `de.the-smarthome.jolt` und einen
   API-Schlüssel für den Upload anlegen.
3. Zertifikat, Profil und API-Schlüssel als Repository-Geheimnisse
   hinterlegen und den Ablauf um einen signierten Archivbau mit
   anschliessendem Upload ergänzen.

Erst danach ist die App auf dem Telefon. Bis dahin beantwortet die CI nur,
ob sie sich bauen lässt — was nicht wenig ist, aber eben noch nichts fährt.

---

## Was dieses Dokument nicht klärt

- **Wie viel der Web-Oberfläche später verschwindet.** Sinnvoll erst zu
  entscheiden, wenn die App im Alltag trägt.
- **Ob das iPad als Testgerät taugt.** Nur die Mobilfunk-Ausführungen haben
  einen echten GPS-Empfänger; reine WLAN-Modelle schätzen die Position und sind
  für eine Fahrtaufzeichnung unbrauchbar.
- **Ob Apple das CarPlay-Entitlement erteilt.** Bis dahin ist Schritt 6 offen,
  und alles davor hängt nicht daran.
