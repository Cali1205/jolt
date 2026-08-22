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
  die Fahrt mit einstellbarem Mehrverbrauch ab, damit sich das ohne Auto
  prüfen lässt.

**Noch nicht da** (im Konzept vollständig beschrieben): die automatische
Umplanung während der Fahrt und echte Verfügbarkeitsdaten. Was jolt *erkennt*,
ist bereits vollständig: dass neu geplant werden müsste, und warum — nur den
neuen Plan zieht unterwegs noch niemand von selbst.

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

---

## Konfiguration

| Variable | Bedeutung |
|---|---|
| `DATABASE_URL` | Fehlt sie, wird SQLite benutzt (`jolt_dev.db`). |
| `APP_PASSWORT` | Zugang zur App. **Leer heisst: kein Login.** Steht beim Start als Warnung im Log. |
| `ORS_API_KEY` | openrouteservice. Fehlt er, greift das Demo-Routing. |
| `OCM_API_KEY` | Nur für den Open-Charge-Map-Import. |
| `TRUSTED_PROXIES` | IPs des Reverse Proxy, die `X-Forwarded-For` setzen dürfen. |
| `RATE_LIMIT_PER_MIN` | Anfragen je Minute und IP (Standard 120). |
| `ENABLE_API_DOCS` | `1` schaltet `/api/docs` frei. Standard: aus. |

---

## Prüfen

Beide Skripte laufen ohne Netz, ohne Postgres und ohne API-Schlüssel:

```bash
./tools/check_modell.py     # Physik: Luftdichte, v², Steigung, Pass, Kälte, Ladekurve
./tools/check_optimierer.py # Ladeplanung: Reserve, Lücken, Säulenwahl, Ausweich
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
  live/       sitzung.py · kanal.py (WebSocket) · simulator.py
  routers/    auth · fahrzeuge · route (inkl. /ladeplan) · saeulen · live
frontend/     index.html · karte.js (eigene Schiebekarte) · route.js · live.js
tools/        import_bnetza.py · import_ocm.py · check_modell.py
              check_optimierer.py · check_backend.py
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

1. **Automatische Umplanung** während der Fahrt, mit Push aufs Telefon.
   Die Auslöser stehen im Konzept (2.3), das Erkennen läuft bereits — was
   fehlt, ist der Weg vom erkannten Abweichen zum neuen Plan.
2. **Echte Fahrzeugdaten** statt Handeingabe. Das Datenmodell
   (`LiveSitzung` / `LivePunkt`) ist bereits darauf ausgelegt: Es ändert sich
   nichts am Schema, nur die Quelle der Messpunkte.
3. **Kalibrierung aus echten Fahrten** — das Gerüst steht in
   `energie/kalibrierung.py`.
