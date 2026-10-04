# jolt als App aufs iPhone — Einrichtung ohne Mac

Dieses Dokument führt einmalig durch alles, was Apple verlangt, bevor
`.github/workflows/ios-testflight.yml` eine signierte App nach TestFlight
schicken kann. **Es braucht keinen Mac.** Alles unten läuft im Browser und mit
`openssl` auf irgendeinem Rechner — auch auf dem unRAID-Server.

Danach ist der Alltag ein Klick: *Actions → iOS TestFlight → Run workflow*.

> **Stand.** Die Dateien im Repository sind fertig und in sich geprüft
> (`tools/check_ios.py`), aber ein signierter Bau lässt sich ohne
> Developer-Konto nicht ausprobieren. Der erste echte Lauf ist deshalb der
> Test; wenn er scheitert, steht im Abschnitt [Wenn etwas schiefgeht](#wenn-etwas-schiefgeht),
> woran es meist liegt.

## Was du am Ende hast

- Die App `jolt` auf dem iPhone, installiert über die Apple-App **TestFlight**.
- Bluetooth zum OBD-Dongle nativ über CoreBluetooth statt über Bluefy.
- Eine Oberfläche, die weiter **vom Server** kommt (`server.url` in
  `capacitor.config.json`): Eine geänderte Zeile in `live.js` geht wie bisher
  über den Container und braucht **keine** neue App-Fassung. Neu hochladen
  musst du nur, wenn sich die Hülle ändert — Plugins, Berechtigungen, Symbol.

## Bundle-ID

Die App-ID ist **`de.thesmarthome.jolt`** (ohne Bindestrich). Apple erlaubt
Bindestriche, aber Capacitor lehnt sie ab (`cap add ios` bricht mit „Invalid
App ID" ab) — mit der ursprünglich geplanten ID (Bindestrich in „the-smarthome") ist der
iOS-Lauf auf `main` deshalb nie grün geworden. Die ID steht an genau einer Stelle: `capacitor.config.json`.
Der Workflow liest sie von dort; ändern musst du sie nur, wenn du eine andere
willst — dann in Schritt 2 dieselbe eintragen.

---

## 1. Apple-Developer-Programm

<https://developer.apple.com/programs/enroll/> — 99 $/Jahr, als Privatperson
reicht. Die Freischaltung dauert Stunden bis zwei Tage.

Danach merken: die **Team-ID** (10 Zeichen, Großbuchstaben und Ziffern) unter
<https://developer.apple.com/account> → *Membership details*.

## 2. App-ID anlegen

<https://developer.apple.com/account/resources/identifiers/list> → **+** →
*App IDs* → *App* → weiter.

- Description: `jolt`
- Bundle ID: *Explicit*, `de.thesmarthome.jolt`
- Capabilities: **keine** ankreuzen. Bluetooth und Standort stehen in der
  `Info.plist` (`tools/ios_info_plist.sh`), nicht als Berechtigung hier.

## 3. und 4. nur für den manuellen Weg

**Standard ist der automatische Weg: Du brauchst weder Zertifikat noch Profil.**
Der Workflow meldet sich mit dem API-Schlüssel aus Schritt 6 bei Apple an, und
Apple legt beides selbst an („Cloud Managed Signing"). Springe dann direkt
zu Schritt 5 und 6. Die Abschnitte 3 und 4 sind der **Rückfall**, falls der
automatische Weg scheitert (siehe Fehlertabelle): Sind die drei zusätzlichen
Geheimnisse aus Schritt 7 gesetzt, signiert der Workflow damit.

### 3. Distributionszertifikat — mit `openssl`

Das Zertifikat belegt gegenüber Apple, dass der Bau von dir stammt. Den
privaten Schlüssel dazu erzeugst du selbst; er verlässt deinen Rechner nur
als Repository-Geheimnis.

```bash
mkdir -p ~/jolt-signatur && cd ~/jolt-signatur

# Privater Schlüssel + Signieranfrage (CSR)
openssl genrsa -out jolt.key 2048
openssl req -new -key jolt.key -out jolt.csr \
  -subj "/emailAddress=DEINE@MAIL.DE/CN=jolt Distribution/C=DE"
```

Im Portal: <https://developer.apple.com/account/resources/certificates/add> →
**Apple Distribution** → `jolt.csr` hochladen → die erzeugte
`distribution.cer` herunterladen. Dann zur `.p12` zusammenfügen, die
GitHub braucht:

```bash
openssl x509 -inform DER -in distribution.cer -out distribution.pem

# OpenSSL 3 (die meisten aktuellen Systeme): -legacy ist nötig, sonst
# scheitert `security import` auf dem Mac-Läufer mit "MAC verification failed".
# OpenSSL 1.1 kennt die Option nicht - dann weglassen.
openssl pkcs12 -export -legacy -inkey jolt.key -in distribution.pem \
  -name "jolt Distribution" -out jolt.p12
# Es fragt nach einem Passwort. Ein langes, zufälliges - es wird das
# Geheimnis IOS_CERT_PASSWORD.
```

Pro Konto sind nur wenige Distributionszertifikate möglich, und es läuft nach
einem Jahr ab. Nach Ablauf diesen Schritt und Schritt 4 wiederholen.

### 4. Bereitstellungsprofil

<https://developer.apple.com/account/resources/profiles/add> → *Distribution*
→ **App Store Connect** → App-ID `de.thesmarthome.jolt` → das Zertifikat aus
Schritt 3 → Name: `jolt AppStore` → `jolt_AppStore.mobileprovision`
herunterladen.

Es muss ein **App-Store**-Profil sein, kein Development- oder Ad-hoc-Profil.
Der Workflow prüft das und sagt es, wenn nicht.

## 5. Die App in App Store Connect anlegen

<https://appstoreconnect.apple.com/apps> → **+** → *Neue App*.

- Plattform iOS, Name `jolt`, Hauptsprache Deutsch
- Bundle-ID: `de.thesmarthome.jolt` (erscheint in der Liste, sobald
  Schritt 2 durch ist)
- SKU: beliebig, z. B. `jolt`

Ohne diesen Eintrag lehnt App Store Connect den Upload ab.

## 6. API-Schlüssel für den Upload

<https://appstoreconnect.apple.com/access/integrations/api> → *Team Keys* →
**+** → Name `github-ci`, Zugriff **Admin**. Nur mit Admin darf Apple für den
automatischen Weg Zertifikate anlegen; App Manager reicht fürs bloße Hochladen.

Die `.p8`-Datei lässt sich **nur einmal** herunterladen. Dazu merken: die
**Key-ID** (Spalte in der Liste) und die **Issuer-ID** (oben auf der Seite).

Der API-Schlüssel ersetzt dein Apple-ID-Passwort samt Zwei-Faktor — so kann
der Lauf hochladen, ohne dass jemand einen Code eintippt.

## 7. Geheimnisse im Repository

GitHub → Repository → *Settings → Secrets and variables → Actions → New
repository secret*.

**Automatischer Weg — diese vier genügen:**

| Name | Inhalt |
|---|---|
| `APPLE_TEAM_ID` | die Team-ID aus Schritt 1 |
| `APPSTORE_KEY_ID` | die Key-ID aus Schritt 6 |
| `APPSTORE_ISSUER_ID` | die Issuer-ID aus Schritt 6 |
| `APPSTORE_KEY_P8_BASE64` | die `.p8` aus Schritt 6, base64-codiert |

**Manueller Rückfall — zusätzlich diese drei, nur alle zusammen:**

| Name | Inhalt |
|---|---|
| `IOS_CERT_P12_BASE64` | `jolt.p12`, base64-codiert (siehe unten) |
| `IOS_CERT_PASSWORD` | das Passwort aus Schritt 3 |
| `IOS_PROFILE_BASE64` | die `.mobileprovision` aus Schritt 4, base64-codiert |

Sind die drei gesetzt, nimmt der Workflow sie und signiert manuell; fehlen
alle, signiert Apple. Zwei von dreien sind ein Fehler, den der erste Schritt
meldet.

Base64 ohne Zeilenumbrüche (für die `.p8`, beim Rückfall auch für `.p12` und
`.mobileprovision`):

```bash
base64 -w0 AuthKey_XXXXXXXXXX.p8       # Linux / unRAID
base64 -i AuthKey_XXXXXXXXXX.p8        # macOS
```

Die Ausgabe in die Zwischenablage und ins Geheimnis-Feld. **Nirgendwo
anders hin** — nicht in einen Chat, nicht ins Repository, nicht in eine
`.env`. Wer `jolt.p12` samt Passwort oder die `.p8` hat, kann Apps in deinem
Namen signieren und hochladen. Nach dem Eintragen die Dateien aus
`~/jolt-signatur` löschen oder in einen Passwortmanager legen; GitHub zeigt
ein Geheimnis nach dem Speichern nicht mehr an.

## 8. Der erste Lauf

*Actions → iOS TestFlight → Run workflow*. Ein Lauf dauert um die zehn
Minuten und zählt wegen des Mac-Läufers **zehnfach** gegen das
Minutenkontingent privater Repositories (siehe `ios.yml`) — also rund
hundert Minuten. Nicht aus Neugier anstoßen.

Der erste Schritt prüft, ob alle sieben Geheimnisse da sind und sagt, welche
fehlen. Danach: Projekt erzeugen → Zertifikat und Profil einrichten →
archivieren → hochladen.

Wenn der Lauf grün ist, verarbeitet App Store Connect den Bau noch einige
Minuten (Status in der App unter *TestFlight*). Dann:

1. In App Store Connect → *Benutzer und Zugriff* sicherstellen, dass du als
   Benutzer eingetragen bist (als Kontoinhaber bist du es).
2. *TestFlight → Interne Tests* → Gruppe anlegen → dich hinzufügen → den
   Bau zuweisen. Interne Tester brauchen **keine** Prüfung durch Apple.
3. Auf dem iPhone die App **TestFlight** installieren, mit derselben
   Apple-ID anmelden → `jolt` erscheint → installieren.

Weitere Personen im Haushalt: in *Benutzer und Zugriff* einladen, dann wie
in 2 hinzufügen. Bis 100 interne Tester, ohne Prüfung.

## Was danach passiert

- **Neue Fassung der Hülle:** Änderung nach `main`, dann den Workflow noch
  einmal starten. Die Build-Nummer ist Lauf-Nummer × 10 + Versuch und
  steigt von selbst.
- **Neue Version der Oberfläche:** nichts. Sie kommt vom Server.
- **TestFlight-Baue laufen nach 90 Tagen ab.** Dann einfach neu hochladen.
- **Zertifikat und Profil laufen nach einem Jahr ab** — Schritte 3 und 4 mit
  neuen Dateien wiederholen, die zwei Geheimnisse ersetzen.

---

## Wenn etwas schiefgeht

| Meldung | Ursache |
|---|---|
| `Es fehlen Repository-Geheimnisse: …` | Schritt 7 nicht vollständig; der Name muss genau stimmen. |
| `Von IOS_CERT_P12_BASE64, … sind nur 1 von 3 gesetzt` | Manueller Rückfall halb eingerichtet: alle drei Geheimnisse setzen oder alle löschen. |
| `No signing certificate … found` / `Communication with Apple failed` im automatischen Modus | Schlüsselrolle ist nicht **Admin** (Schritt 6), oder Key-ID/Issuer-ID sind vertauscht. |
| `Your team has no devices from which to generate a provisioning profile` | Der automatische Weg braucht beim Archivieren ein Entwicklungsprofil und damit ein registriertes Gerät. Entweder das iPhone unter [Geräte](https://developer.apple.com/account/resources/devices/list) eintragen (UDID) oder auf den manuellen Rückfall (Schritt 3, 4, 7) wechseln. |
| `MAC verification failed` beim Import (manuell) | `.p12` ohne `-legacy` erzeugt (Schritt 3). |
| `Das Profil ist für … die App heisst …` | Profil gehört zu einer anderen Bundle-ID als `capacitor.config.json`. |
| `Das Profil enthält eine Geräteliste` | Development-/Ad-hoc-Profil statt App Store (Schritt 4). |
| `No signing certificate "Apple Distribution" found` | Das Zertifikat im Profil ist nicht das aus der `.p12`. Beide aus demselben Durchgang. |
| `Erwartet: zwei Stellen mit 'CODE_SIGN_STYLE = Automatic;'` | Capacitor hat seine Vorlage geändert; `tools/ios_signatur.sh` anpassen. |
| `Invalid App ID` bei `cap add ios` | Bindestrich oder Ziffer am Segmentanfang in `appId`. |
| Upload `Unable to authenticate` | Key-ID/Issuer-ID vertauscht oder Rolle des Schlüssels zu schwach (Schritt 6). |
| Upload `bundle version must be higher` | Build-Nummer = Lauf-Nummer × 10 + Versuch; sie sinkt nur, wenn der Workflow gelöscht und neu angelegt wurde. Dann die Nummer in `ios-testflight.yml` um einen festen Betrag anheben. |
| Upload `Invalid large app icon` | `tools/ios-app-icon.png` hat einen Alphakanal oder ist nicht 1024 × 1024; `tools/check_ios.py` prüft das. |

## Offen

- **Hintergrund-Standort** (Schritt 5 im Konzept): Die `Info.plist` kennt den
  Modus `location` schon, aber es gibt noch kein Plugin, das ihn nutzt. Bis
  dahin zeichnet die App bei gesperrtem Bildschirm nur über Bluetooth auf,
  nicht über GPS.
- **Warteschlange gegen Funklöcher** (Schritt 6): ebenfalls noch nicht gebaut.
- **Push-Benachrichtigungen** laufen weiter über Web-Push im Browser; die App
  hat keine Push-Berechtigung (das bräuchte APNs und eine Capability).

Beide Punkte lassen sich erst sinnvoll bauen, wenn die App auf dem Telefon
liegt: Ob ein Hintergrund-Plugin unter iOS wirklich weiterläuft, zeigt nur
eine Fahrt.
