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

Ein WebView-Wrapper (Capacitor) löst davon **nichts** von selbst: Er benutzt
dasselbe WebKit und hat dieselbe fehlende Bluetooth-API. Er würde für jeden
der drei Punkte ein natives Plugin brauchen — und dann ist der Weg zum
richtigen nativen Client kürzer als der Umweg.

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

| # | Schritt | Ergebnis |
|---|---|---|
| 0 | CarPlay-Entitlement beantragen | läuft im Hintergrund weiter |
| 1 | Xcode-Projekt, Netz-Schicht, Anmeldung | App zeigt die Fahrtenliste |
| 2 | Planungsansicht | Route rechnen und speichern geht |
| 3 | OBD über CoreBluetooth + Protokolltests | Ladestand aus dem Auto, ohne Bluefy |
| 4 | Live-Aufzeichnung mit Hintergrund-Standort | Fahrt mit Telefon in der Tasche |
| 5 | Warteschlange gegen Funklöcher | keine Lücken mehr |
| 6 | CarPlay-Statusvorlage | sobald das Entitlement da ist |

Schritt 3 ist der Prüfstein: Ist der Ladestand aus dem Auto einmal nativ
gelesen und stimmt er mit dem überein, was die PWA über Bluefy liefert, ist
das grösste Risiko des Umbaus erledigt.

---

## Bauen und Ausliefern

**Entwicklung** auf dem MacBook Air (2018+, Intel, 16 GB). Reicht für dieses
Projekt — Intel ist bei Builds und SwiftUI-Vorschauen spürbar langsamer als
Apple Silicon, aber es ist kein Hindernis. Fernzugriff vom Windows-Rechner
über die eingebaute Bildschirmfreigabe; ein eigenes macOS-Benutzerkonto für
die Entwicklung verhindert, dass sich zwei Leute eine Sitzung teilen.

**Der CarPlay-Simulator läuft in Xcode**, ein echtes Auto-Display braucht es
zum Entwickeln nicht.

**Automatisch bauen** über GitHub Actions:
`.github/workflows/ios.yml` baut bei jeder Änderung unter `ios/` gegen den
Simulator und lässt die Protokolltests laufen. Der Ablauf ist mit einem
Pfadfilter versehen und rührt sich nicht, solange es `ios/` noch nicht gibt.

Läuft auf `macos-latest`, und dafür gilt bei privaten Repositories ein
**Minutenfaktor von 10** — eine Minute auf einem Mac-Läufer zählt wie zehn
gegen das Kontingent. Deshalb baut der Ablauf nur gegen den Simulator und
signiert nicht: Ein Simulatorbau braucht weder Zertifikat noch
Bereitstellungsprofil und ist der billigste Weg, „compiliert überhaupt noch"
zu beantworten.

**Auf Geräte kommt die App über TestFlight.** Damit braucht kein fremdes
iPhone je ein Kabel zum Mac. Einmal muss das eigene Entwicklungsgerät per
Kabel angeschlossen werden, damit Xcode ihm vertraut; danach geht auch das
über WLAN.

**Ein Apple-Developer-Programm für 99 $/Jahr ist Pflicht** — ohne läuft eine
selbst gebaute App nur sieben Tage auf dem Gerät, und weder TestFlight noch
CarPlay-Entitlements gibt es. Das ist der Punkt, an dem der Umbau Geld kostet.

---

## Was dieses Dokument nicht klärt

- **Wie viel der Web-Oberfläche später verschwindet.** Sinnvoll erst zu
  entscheiden, wenn die App im Alltag trägt.
- **Ob das iPad als Testgerät taugt.** Nur die Mobilfunk-Ausführungen haben
  einen echten GPS-Empfänger; reine WLAN-Modelle schätzen die Position und sind
  für eine Fahrtaufzeichnung unbrauchbar.
- **Ob Apple das CarPlay-Entitlement erteilt.** Bis dahin ist Schritt 6 offen,
  und alles davor hängt nicht daran.
