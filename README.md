# jolt — Routenplaner für Elektroautos

Plant Ladestopps und **zieht den Plan während der Fahrt nach**. Das ist der
Punkt: Ein Plan, der bei Abfahrt gerechnet wurde, ist nach achtzig Kilometern
falsch — Tempo, Temperatur, Wind und Stau addieren sich in dieselbe Richtung.
Wer das merkt, braucht keinen Sicherheitspuffer von zwanzig Prozent.

**Stack:** FastAPI + PostgreSQL (Docker, SQLite-Fallback lokal) · Vanilla-JS-PWA
ohne Build-Schritt · openrouteservice fürs Routing mit Höhenprofil ·
Bundesnetzagentur und Open Charge Map für die Ladepunkte · Open-Meteo fürs Wetter

Das ausführliche Konzept mit der Begründung jeder Entscheidung steht in
**[konzept-routenplaner.md](konzept-routenplaner.md)**.

---

## Was jolt heute kann

- **Verbrauch physikalisch rechnen** statt pauschal in kWh/100 km — Luft-
  widerstand mit v², Steigung aus dem Höhenprofil, Rekuperation mit realistischem
  Wirkungsgrad, Heizung nach Zeit statt nach Strecke. Der Unterschied zwischen
  110 und 130 km/h sind 40 % Luftwiderstand, nicht 18; das muss ein Planer
  abbilden können, sonst ist er Kosmetik.
- **Fahrzeugprofile mit Ladekurve** — inklusive Vorlagen, damit niemand am
  ersten Tag einen cw-Wert raten muss.
- **Ladesäulen importieren** aus dem amtlichen Register der Bundesnetzagentur
  und aus Open Charge Map. Beide Importe sind idempotent.
- **Reichweitenmarke auf der Karte** — der Punkt, an dem der Ladestand die
  Reserve erreicht. Schon ohne Ladestopp-Planung die Antwort auf die Frage,
  die vor der Abfahrt zählt.
- **Ladepunkte im Korridor** mit Umwegzeit in Minuten, sortiert nach
  Fortschritt entlang der Route.
- **Ladestopps planen** — die zeitoptimale Folge von Stopps und Lademengen:
  Pareto-Dijkstra über `(Ladepunkt, Ankunfts-SoC)`, anschliessend wandern die
  Ladehübe auf feinem Raster in den steilen Teil der Ladekurve. Zu jedem Stopp
  steht der Ausweichstandort dabei, der ohne Nachladen noch erreichbar ist.
  Eine Etappe gilt nur als fahrbar, wenn der Ladestand *unterwegs* über der
  Reserve bleibt — über einen Pass sieht die Bilanz am Ende sonst harmlos aus.
- **Live-Nachführung** — Messpunkte herein, Ist gegen Soll, laufender
  Verbrauchsfaktor, und die Reserve-Marke wandert mit. Ein Simulator spielt
  die Fahrt mit einstellbarem Mehrverbrauch *und* einstellbarer Fahrzeit ab,
  damit sich das ohne Auto prüfen lässt.
- **Mit dem gemessenen Tempo rechnen statt mit dem geratenen.** Der Regler vor
  der Abfahrt ist eine Schätzung; das Telefon weiss es besser. Solange noch
  kein Ladestand gemeldet wurde, wird die Reststrecke deshalb nicht skaliert,
  sondern **neu gerechnet** — mit dem tatsächlich gefahrenen Tempo und dem
  Wetter von jetzt statt von der Abfahrt. Ein Faktor täte es hier nicht:
  Luftwiderstand geht mit v², Rollwiderstand nahezu linear, die
  Nebenverbraucher gar nicht mit dem Tempo, sondern mit der Zeit — und die
  *sinkt*, wenn man schneller fährt. Sobald ein gemessener Verbrauch vorliegt,
  gilt der, denn er enthält die Wirkung des Tempos schon.
- **Umplanen während der Fahrt** — das eigentliche Ziel des Projekts. Greift
  einer der Auslöser, wird die Reststrecke ab der aktuellen Position neu
  geplant: mit dem gemessenen Verbrauch, der gemessenen Fahrzeit und dem
  Ladestand, der wirklich da ist. Ändert sich dabei etwas, sagt jolt es in
  einem Satz — und sonst schweigt es.

| Auslöser | Schwelle |
|---|---|
| Nächster Ladepunkt als belegt gemeldet | sofort |
| Reserve wird vor dem Ziel erreicht | sofort |
| Mehr als 500 m neben der Route | ab 1 Minute |
| Ankunfts-Ladestand am nächsten Stopp weicht ab | > 5 Prozentpunkte |
| Ankunftszeit verschiebt sich (Stau) | > 10 Minuten |

- **Benachrichtigung aufs Telefon**, wenn sich der Plan ändert — auch bei
  dunklem Bildschirm, über Web Push. Und nur dann: Eine Meldung, die bei jeder
  Messung kommt, schaltet man nach zehn Minuten ab.
- **Lernen aus gefahrenen Fahrten** — am Ende jeder Fahrt wird der
  Korrekturfaktor des Fahrzeugs fortgeschrieben, gedämpft, damit eine einzelne
  Fahrt mit Dachbox ihn nicht dauerhaft verbiegt.
- **Anschluss für einen Logger im Auto** — ein Gerät, das fest im Fahrzeug
  sitzt, kann die Sitzungs-ID einer Fahrt nicht kennen; die entsteht erst beim
  Losfahren in der App und wechselt mit jeder Fahrt. Es weist sich deshalb mit
  einem langlebigen **Logger-Token des Fahrzeugs** aus (`POST
  /api/live/melden`), und jolt sucht die laufende Fahrt selbst. Steht das Auto,
  ist das kein Fehler, sondern eine Antwort mit `aufgenommen: false` — ein
  unbeaufsichtigtes Gerät, das auf Fehlerantworten stösst, protokolliert Fehler
  oder schaltet sich ab.

**Noch nicht da**: echte Verfügbarkeitsdaten (siehe unten) und der Übersetzer,
der einem OBD2-Dongle den Ladestand entlockt (siehe „Nächste Schritte").

---

## Loslegen

### Lokal, ohne alles

```bash
pip install -r backend/requirements.txt
cd backend && python -m uvicorn app.main:app --reload --port 8322
```

Läuft gegen SQLite und ohne API-Schlüssel. Ohne `ORS_API_KEY` rechnet jolt mit
**erfundenen Demo-Routen** — die Kette lässt sich damit vollständig durchspielen,
aber die Strecke ist die Luftlinie. Die Oberfläche sagt es an jeder Stelle dazu.

### Mit echten Routen

Kostenlosen Schlüssel holen (2.500 Anfragen/Tag):
<https://openrouteservice.org/dev/#/signup>

```bash
export ORS_API_KEY=…
```

### Im Docker

```bash
cp .env.example .env      # DB_PASSWORD, APP_PASSWORT und ORS_API_KEY eintragen
docker compose up --build
```

Danach unter <http://localhost:8322>. Die Datenbank liegt auf dem Host unter
`/opt/docker/jolt/db` — bei Bedarf in `docker-compose.yml` anpassen.

### Ladesäulen importieren

Die Datei „Ladesäulenregister" (CSV) von der [Ladesäulenkarte der
Bundesnetzagentur](https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html)
herunterladen, dann:

```bash
./tools/import_bnetza.py ladesaeulenregister.csv
OCM_API_KEY=… ./tools/import_ocm.py AT,CH 5000 50   # optional, fürs Ausland
```

Ohne diesen Schritt bleibt die Liste „Ladepunkte entlang der Route" leer — die
Tabelle ist bei einer frischen Installation schlicht noch nicht gefüllt.

`import_ocm.py` fragt Open Charge Map länderweise ab. Bei einem grossen Land
(z.B. Frankreich) blättert OCMs `offset`-Pagination bei sehr vielen Treffern
nicht zuverlässig weiter — ein Teil der Ladepunkte bleibt dann unerreichbar,
egal wie hoch das Limit steht. Für gezielt eine Strecke gibt es die
Alternative `import_ocm_route.py`, die stattdessen mehrere kleinere Umkreise
entlang der tatsächlichen Routen-Geometrie abfragt:

```bash
OCM_API_KEY=… ./tools/import_ocm_route.py <fahrt_id> 30 50   # Umkreis 30 km, ab 50 kW
```

Die `fahrt_id` steht in der Antwort von `GET /api/fahrten`, nachdem die
Strecke einmal in der App berechnet wurde.

### Benachrichtigungen aufs Telefon

```bash
./tools/push_schluessel.py      # erzeugt ein VAPID-Schlüsselpaar
```

Die drei ausgegebenen Zeilen in die `.env` übernehmen und jolt neu starten.
Danach fragt die App beim Start einer Live-Fahrt einmal nach der Erlaubnis.
Ob es funktioniert, sagt `POST /api/push/probe` — die Nachricht muss auf dem
Gerät ankommen, auch bei dunklem Bildschirm.

Zwei Dinge, an denen es sonst scheitert: Der Browser gibt Benachrichtigungen
nur über **HTTPS** frei (`localhost` ausgenommen), und die App muss auf iOS
zum Home-Bildschirm hinzugefügt sein. Der **private** Schlüssel bleibt auf dem
Server; wer ihn hat, kann im Namen dieser Installation an die angemeldeten
Geräte senden.

---

## Konfiguration

| Variable | Bedeutung |
|---|---|
| `DATABASE_URL` | Fehlt sie, wird SQLite benutzt (`jolt_dev.db`). |
| `APP_PASSWORT` | Zugang zur App. **Leer heisst: kein Login.** Steht beim Start als Warnung im Log. |
| `ORS_API_KEY` | openrouteservice. Fehlt er, greift das Demo-Routing. |
| `OCM_API_KEY` | Nur für den Open-Charge-Map-Import. |
| `VAPID_PRIVATE_KEY` | Web Push. Fehlt er, sind Benachrichtigungen aus. |
| `VAPID_PUBLIC_KEY` | Derselbe Schlüssel, öffentliche Hälfte — der Browser braucht ihn. |
| `VAPID_SUBJECT` | `mailto:` oder `https:` — wen der Push-Dienst erreicht. |
| `TRUSTED_PROXIES` | IPs des Reverse Proxy, die `X-Forwarded-For` setzen dürfen. |
| `RATE_LIMIT_PER_MIN` | Anfragen je Minute und IP (Standard 120). |
| `ENABLE_API_DOCS` | `1` schaltet `/api/docs` frei. Standard: aus. |

---

## Prüfen

Alle Skripte laufen ohne Netz, ohne Postgres und ohne API-Schlüssel:

```bash
./tools/check_modell.py     # Physik: Luftdichte, v², Steigung, Pass, Kälte, Ladekurve
./tools/check_optimierer.py # Ladeplanung: Reserve, Lücken, Säulenwahl, Ausweich
./tools/check_quellen.py    # Fremde Meldeformate übersetzen - und Schrott ablehnen
./tools/check_umplanung.py  # Live: Auslöser einzeln, Umplanung über die ganze Kette
./tools/check_push.py       # Web Push: Schlüssel, Verschlüsselung, Abos, Aufräumen
./tools/check_backend.py    # ganze Kette: Schema, Import, Route, Korridor, Ladeplan, Live
```

`check_modell.py` prüft nicht auf feste Zahlen, sondern auf die Verhältnisse,
die gelten müssen — etwa dass 130 km/h mehr als 10 % über 110 km/h liegen, aber
unter dem reinen v²-Faktor, oder dass ein Pass mehr kostet als die Ebene, obwohl
man wieder auf Ausgangshöhe ankommt.

`check_optimierer.py` glaubt dem Planer nichts: Ein zweiter, unabhängiger
Nachrechner fährt jeden fertigen Plan Kilometer für Kilometer ab und sieht nach,
ob der Ladestand irgendwo unter die Reserve fällt. Geprüft wird ausserdem gegen
die beiden Fälle, an denen ein gieriger Planer scheitert — eine lange Lücke ohne
Schnelllader und die Wahl zwischen einer nahen schwachen und einer weiteren
starken Säule.

`check_quellen.py` prüft vor allem das, was schiefgeht. Eine Meldung, die
stimmt, ist der langweilige Fall; interessant sind das fehlende Feld, der
Zeitstempel in Millisekunden statt Sekunden und der aus dem Jahr 1970, wenn
ein Kleinstrechner ohne Netz startet. Fremde Daten sind bis zum Beweis des
Gegenteils kaputt, und ein Übersetzer, der das nicht abfängt, verlagert den
Fehler nur — er landet dann als 500er im Log oder, schlimmer, als stiller
Unsinn im Energieprofil.

`check_umplanung.py` prüft jeden Auslöser einzeln — über seiner Schwelle muss
er greifen, darunter schweigen; ein Auslöser, der immer feuert, ist so nutzlos
wie einer, der es nie tut. Danach die ganze Kette: Fahrt rechnen, Ladepunkte
anlegen, mit Mehrverbrauch und mit Stau abspielen und nachsehen, ob der Plan
sich ändert, gültig bleibt und sich *nicht* bei jeder Messung ändert.

Zuletzt eine Fahrt, in der wirklich **geladen** wird. Der Simulator tut das
nie — sein Ladestand fällt monoton bis null —, und deshalb blieb der
Normalfall jeder Langstrecke ungeprüft: anhalten, laden, weiterfahren. Das
Energieprofil führt ausschliesslich Fahrzeit; die Ladezeit steht im Plan. Wer
die Wanduhr ungefiltert dagegen hält, meldet nach dem ersten Ladestopp eine
Verspätung in Höhe der Ladedauer — dauerhaft, denn aufgeholt wird sie nie.
Der Auslöser „Ankunft verschiebt sich" stünde damit für den Rest der Fahrt
über seiner Schwelle. Eine Meldung, die immer kommt, schaltet man ab.

`check_push.py` prüft alles vor dem Netzsprung — und der Rundlauf durch die
Verschlüsselung ist der Kern: Die Nutzlast wird für ein nachgebautes
Browser-Abo verschlüsselt und mit dessen privatem Schlüssel wieder
entschlüsselt. Kommt der Klartext zurück, stimmt der Pfad nach RFC 8291. Was
das Skript **nicht** prüft, ist der Sprung zum Push-Dienst selbst; dafür gibt
es `POST /api/push/probe` mit einem echten Gerät.

`check_backend.py` fährt eine simulierte Strecke mit 25 % Mehrverbrauch und
prüft, dass die Reserve-Marke nach vorn rückt. Das ist der Prüfstein der
Live-Funktion.

---

## Aufbau

```
konzept-routenplaner.md   Das Konzept mit der Begründung jeder Entscheidung
backend/app/
  routing/    provider.py (Interface) · ors.py · demo.py · korridor.py
  energie/    modell.py · wetter.py · kalibrierung.py
  laden/      kurven.py · optimierer.py · saeulen_import.py · verfuegbarkeit.py
  live/       sitzung.py · umplanung.py · kanal.py (WebSocket) · simulator.py
              quellen/  fremde Meldeformate übersetzen (jolt.py · abrp.py)
  push.py     Web Push: Schlüssel, Abos, Versand
  routers/    auth · fahrzeuge · route (inkl. /ladeplan) · saeulen · live · push
frontend/     index.html · karte.js (eigene Schiebekarte) · route.js · live.js
              sw.js (Offline-Gerüst und Push-Empfang)
tools/        import_bnetza.py · import_ocm.py · import_ocm_route.py
              push_schluessel.py
              check_modell.py · check_optimierer.py · check_umplanung.py
              check_push.py · check_backend.py
```

**Der Optimierer kennt weder Datenbank noch Netz.** Er bekommt ein fertig
gerechnetes Streckenprofil und eine Liste von Ladeoptionen — mehr braucht er
nicht. Das ist der Grund, warum `check_optimierer.py` ohne beides auskommt und
ein hypothetischer Standort („was wäre, wenn hier ein 300-kW-Lader stünde?")
eine Zeile Code ist statt eines Datenbankeintrags.

Möglich wird das durch eine Eigenschaft des Verbrauchsmodells: Der
Energiebedarf einer Etappe hängt **nicht** vom Ladestand ab — ein E-Auto wird
beim Laden nicht schwerer. Ein einziger Durchlauf des Modells genügt also für
alle Varianten; der Optimierer liest den Bedarf jeder Etappe als Differenz
zweier kumulierter Werte ab. Deshalb kostet ein zweiter Ladeplan mit anderem
Radius weder eine Routing- noch eine Wetterabfrage.

**Kein PostGIS.** Der einzige Geo-Query ist „alle Ladepunkte im Korridor um eine
Polyline". Das löst ein Index auf `(lat, lon)` mit Bounding-Box-Vorfilter und
Haversine bei rund 150.000 deutschen Ladepunkten in Millisekunden — und erhält
den SQLite-Fallback für die lokale Entwicklung.

**Keine Kartenbibliothek.** `frontend/karte.js` sind zweihundert Zeilen für
Kacheln, eine Linie, Marker und Zoomen mit Ziehen. Eine Bibliothek einzubinden
hiesse, sie mit ins Repo zu legen (die Content-Security-Policy verbietet CDNs)
und dauerhaft zu pflegen — für einen Bruchteil ihres Funktionsumfangs.

Kartenkacheln kommen von OpenStreetMap; die Namensnennung steht unter der Karte,
weil sie verlangt ist.

---

## Nächste Schritte

**Echte Fahrzeugdaten** statt Handeingabe — der Weg dorthin ist offen, das
Datenmodell (`LiveSitzung` / `LivePunkt`) nimmt sie unverändert entgegen.
Was noch fehlt, ist der Übersetzer: Ein ELM327-Dongle liest den Ladestand
eines MEB-Fahrzeugs nicht über die genormten OBD2-PIDs — die sind auf
Verbrennungsmotoren gemünzt —, sondern über herstellerspezifische
UDS-Abfragen. Diese Kenntnis kauft man sich sinnvollerweise über bestehende
Software ein, statt sie nachzubauen.

Die Übersetzerschicht dafür **steht**: `live/quellen/` normalisiert fremde
Telemetrieformate auf einen `Rohpunkt`, und `POST /api/live/melden` nimmt sie
mit `format: "…"` entgegen. Mitgeliefert sind jolts eigenes Format und das von
Iternio (ABRP) — letzteres, weil es im Umfeld der Elektroauto-Logger so etwas
wie ein Quasi-Standard ist: die ABRP-App selbst, ESP32-Dongles wie der WiCAN,
OVMS und diverse Bastelskripte sprechen es. Ein Übersetzer dafür ist deshalb
kein Adapter für einen Anbieter, sondern einer für ein halbes Ökosystem.

**Die Übersetzung kennt kein Netz** — sie bekommt ein geparstes Objekt und gibt
einen `Rohpunkt` zurück. Deshalb läuft `check_quellen.py` ohne Netz, ohne
Datenbank und ohne Zugangsdaten, und deshalb lässt sich ein neues Format anhand
einer aufgezeichneten Antwort einbauen, ohne im Auto zu sitzen.

Was noch fehlt, ist der Transport: das Stück, das die Daten tatsächlich vom
Dongle bis zu diesem Endpunkt bringt. Für ein iPhone führt der einzige heute
gangbare Weg über die ABRP-App als Sensortreiber (sie liest BLE-Dongles live
aus) und deren Telemetrie-API; Car Scanner scheidet dort aus, weil es auf iOS
nur Aufzeichnungsdateien exportiert und keine Live-Ausgabe hat.

Ein Detail, das dabei zählt: Fahrzeuge auf MEB-Basis liefern **zwei**
Ladestände — den der Anzeige und den des Batteriemanagements. `reserve_soc`
und `ziel_soc` meinen den der Anzeige, und der Adapter muss den richtigen von
beiden nehmen; sonst rechnet das Modell dauerhaft um den verborgenen Puffer
daneben.
