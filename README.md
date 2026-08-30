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
  **Ein Halt kostet fünf Minuten, bevor das erste Elektron fliesst** —
  einparken, Kabel, freischalten. Ohne diesen Posten ist die Zielfunktion
  blind für die Anzahl der Stopps, und weil ein Akku bei 10 % viel schneller
  lädt als bei 60 %, wird es dann immer günstiger, dieselbe Energie auf viele
  kurze Halte zu verteilen. Der Planer tat das auch: zehn Stopps statt vier,
  sechs davon unter vier Minuten — rechnerisch optimal und in Wirklichkeit
  eine Dreiviertelstunde langsamer.
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
  Fahrt mit Dachbox ihn nicht dauerhaft verbiegt. Ladeabschnitte fallen dabei
  heraus: Wer unterwegs vierzig Prozentpunkte nachlädt, sieht am Ende einen
  Verlust, der um diese vierzig zu klein ist.
- **Fahrten aufzeichnen statt planen** — der Weg für den Fall, für den sich
  Planen nicht lohnt: eine bekannte kurze Strecke, ein paarmal gefahren, ist
  die sauberste Messung überhaupt. Strecke, Höhenprofil und Prognose entstehen
  hinterher aus den Messpunkten. Vergisst man das Beenden, macht der Server es
  selbst — bei einer Aufzeichnung wäre es sonst Totalverlust, denn bis dahin
  ist die Fahrt eine Hülle mit leerer Geometrie. Eine Ladepause verlängert die
  Frist, sonst zerschneidet das Aufräumen die Fahrt, die gleich weitergeht.
- **Den Dongle direkt lesen** — über Web Bluetooth, ohne Zwischen-App. Auf iOS
  braucht es dafür den Browser **Bluefy**; Safari kennt Web Bluetooth nicht.
  Alle Messwerte stehen live im Dashboard, samt Alter je Wert: Eine
  eingefrorene Anzeige sieht sonst aus wie eine laufende. Was das Auto liefert
  und wie, steht weiter unten.
- **Die Strecke aus dem Kilometerstand** statt aus dem GPS. Bei
  Zwölf-Sekunden-Takt liegen bei Landstrassentempo hundertsechzig Meter
  zwischen zwei Punkten, und die Luftlinie schneidet jede Kurve ab; ein
  Funkloch reisst gleich ein ganzes Stück heraus. Der Zähler im Auto kennt
  beides nicht. Weil der Verbrauch in kWh **pro hundert Kilometer** gerechnet
  wird, wandert der Fehler sonst direkt in den Korrekturfaktor.
- **Verbrauch aus den Energiezählern des Fahrzeugs.** Sie zählen über die
  Lebensdauer, was in den Akku hinein- und was herausgegangen ist; ihre
  Differenz über ein Stück Fahrt ist die verbrauchte Energie — mit 0,117 Wh
  Auflösung statt der 339 Wh eines Ladestandsschritts. Fast dreitausendmal
  feiner, und deshalb zeigt der Balkenplot den Verbrauch je **Minute** statt
  je fünf.
- **Die gemessene Akkukapazität schlägt die Prospektangabe.** Im Profil steht,
  was der Hersteller für ein neues Fahrzeug angibt; das Auto meldet, was
  dieser Akku heute kann — beim ID.Buzz 73,8 statt 77 kWh nach 60 000 km. An
  dieser Zahl hängt jede Umrechnung zwischen Ladestand und Kilowattstunden.
- **Ladezeit gegen Kosten abwägen.** Ein Zeitwert in Euro je Stunde macht
  beides vergleichbar: Wer zehn Minuten länger lädt, dafür aber einen Stopp
  spart und beim günstigeren Anbieter steht, fährt vielleicht besser. Dazu ein
  Bonus für grosse Ladeparks (das Risiko, vor einer belegten Säule zu stehen,
  sinkt mit der Anzahl) und für bevorzugte Anbieter — beides als Gewicht, nie
  als Ausschluss.
- **Anschluss für einen Logger im Auto** — ein Gerät, das fest im Fahrzeug
  sitzt, kann die Sitzungs-ID einer Fahrt nicht kennen; die entsteht erst beim
  Losfahren in der App und wechselt mit jeder Fahrt. Es weist sich deshalb mit
  einem langlebigen **Logger-Token des Fahrzeugs** aus (`POST
  /api/live/melden`), und jolt sucht die laufende Fahrt selbst. Steht das Auto,
  ist das kein Fehler, sondern eine Antwort mit `aufgenommen: false` — ein
  unbeaufsichtigtes Gerät, das auf Fehlerantworten stösst, protokolliert Fehler
  oder schaltet sich ab.

**Noch nicht da**: Belegungsdaten der Ladepunkte (es gibt sie inzwischen, siehe
„Nächste Schritte") und ein Puffer für Messpunkte, die während eines Funklochs
nicht rausgehen — heute sind sie verloren.

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
Live-Funktion. Dazu hält es fest, was die Prüfskripte selbst nicht sehen
könnten: dass jeder Verweis im HTML eine Version trägt (der Cache-Fehler war
viermal da), dass jeder ausgelesene Messwert eine Beschriftung hat, und dass
jedes Werkzeug in `tools/` das Paket in **beiden** Layouten findet — im Repo
unter `backend/app`, im Image daneben als `app`.

```bash
ORS_API_KEY=… ./tools/probelauf.py   # kein Prüfskript, ein Probelauf
```

`probelauf.py` ist ein anderes Werkzeug als die sechs darüber, und der
Unterschied ist der Zweck. Ein Prüfskript sichert, was man schon weiss; dieser
Lauf soll finden, woran noch niemand gedacht hat. Er behauptet nichts, er
fährt eine echte Route mit echten Ladepunkten ab, zeichnet danach eine Fahrt
auf, wie der Dongle sie schickt — mit Ladepause, Funkloch und Werten, die
einzeln ausfallen — und zeigt am Ende, was dabei nicht stimmt.

Er hat sich gelohnt: Vier Fehler kamen dabei heraus, die keiner der sechs
Prüfläufe gesehen hatte, weil sie alle mit kurzen Fahrten **ohne Ladestopp**
arbeiten. Der teuerste war ein gelernter Faktor, der bei jeder Fahrt mit
Ladestopp zu niedrig ausfiel — und weil er in den Plausibilitätsgrenzen blieb,
fiel es nicht auf.

Ohne Auto prüfen lässt sich noch mehr: Unter `/obd` liest „Alle Werte prüfen"
einmal den vollständigen Satz und hält jeden Wert gegen das, was physikalisch
plausibel wäre. Der schärfste Teil ist der **Kreuzvergleich** — Entladezähler
geteilt durch Kilometerstand ergibt den Lebensdauerverbrauch, und trifft der
12 bis 40 kWh/100 km, stimmen beide Formeln. Zwei unabhängig gelesene Werte
prüfen sich gegenseitig, im Stand. Genau so ist ein Vorzeichenfehler
aufgeflogen, den die Bereichsprüfung durchgelassen hatte.

---

## Aufbau

```
konzept-routenplaner.md   Das Konzept mit der Begründung jeder Entscheidung
backend/app/
  geo.py      Haversine und Peilung - kennt nichts, wird von allen gebraucht
  models.py   SQLAlchemy · database.py · deps.py · security.py
  energie/    modell.py (Physik) · profil.py · wetter.py
              kalibrierung.py · ladephasen.py (Fahrt- und Ladeabschnitte)
  routing/    provider.py (Interface) · ors.py · demo.py · korridor.py
  laden/      optimierer.py · kurven.py · preise.py · verfuegbarkeit.py
              saeulen_import.py
  live/       sitzung.py · umplanung.py · aufzeichnung.py · aufraeumen.py
              kanal.py (WebSocket) · simulator.py
              quellen/  fremde Meldeformate übersetzen (jolt.py · abrp.py)
  push.py     Web Push: Schlüssel, Abos, Versand
  routers/    auth · fahrzeuge · route (inkl. /ladeplan) · saeulen · live · push
frontend/     index.html · core.js · app.js · karte.js (eigene Schiebekarte)
              route.js · live.js · fahrten.js · fahrzeug.js
              obd.html · obd-kern.js · obd.js  (Dongle, eigene Seite)
              sw.js (Offline-Gerüst und Push-Empfang)
tools/        import_bnetza.py · import_ocm.py · import_ocm_route.py
              push_schluessel.py · pruefen.py (Gerüst der Prüfskripte)
              check_modell.py · check_optimierer.py · check_quellen.py
              check_umplanung.py · check_push.py · check_backend.py
              probelauf.py (kein Prüfskript - siehe „Prüfen")
```

**Die Schichten greifen nur nach unten.** `geo` ganz unten (kennt nichts),
darüber `energie`, `routing`, `laden`, `live`, und obenauf die Router. Der
Import-Graph ist zyklenfrei; `geo.py` liegt bewusst neben `models` und nicht
in einer der Schichten, weil sonst `routing` für eine Entfernung in die Physik
greifen müsste oder umgekehrt.

**Der Dongle hat eine eigene Seite.** `/obd` funktioniert nur in einem Browser
mit Web Bluetooth, und ein Bedienelement, das in Safari stumm bleibt, hat in
der Hauptoberfläche nichts verloren. `obd-kern.js` ist der Baustein — die
Liste der Messwerte, der Handshake, das Zusammensetzen mehrteiliger Antworten;
`obd.js` ist die Diagnoseseite darum herum, und `live.js` nutzt denselben
Baustein während der Fahrt.

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

## Was das Auto hergibt — die MEB-Datenkennungen

Ein ELM327-Dongle liest an einem MEB-Fahrzeug (ID.3, ID.4, ID.Buzz, Enyaq,
Q4 e-tron, Cupra Born) **nichts** über die genormten OBD2-PIDs — die sind auf
Verbrennungsmotoren gemünzt. Alles läuft über herstellerspezifische
UDS-Abfragen, und die Kenntnis darüber steht in drei Quellen, die einander
teils widersprechen:

* [spot2000/Volkswagen-MEB-EV-CAN-parameters](https://github.com/spot2000/Volkswagen-MEB-EV-CAN-parameters)
  — 193 Parameter mit Adressen; bei vielen fehlt die Umrechnung
* [meatpiHQ/wican-fw](https://github.com/meatpiHQ/wican-fw/blob/main/vehicle_profiles/vw/ev_meb.json)
  — Fahrzeugprofil mit Formeln, nennt den ID.Buzz ausdrücklich
* [codingABI/id3esp32obd2](https://github.com/codingABI/id3esp32obd2)
  — ESP32-Logger, liest die CAN-Rahmen direkt

Wo sie sich widersprechen, steht unten, welcher Fassung jolt folgt und warum.
Die Liste selbst steht in `frontend/obd-kern.js`; **das ist die
Referenz**, diese Tabelle ist ihre Erläuterung.

### Zieladressen

Ein Fahrzeug spricht auf **zwei Rahmenbreiten**. Das ist der Grund, warum
Klima- und Akkuwerte anfangs gar nicht ankamen: Der Handshake stellt `ATSP7`
ein — 29 Bit —, und auf einer 11-Bit-Kennung hört dann niemand.

| Gerät | Protokoll | ATCP | ATSH | ATCRA | ATFCSH |
|---|---|---|---|---|---|
| Batterie (BMS) | 7 (29 Bit) | `17` | `FC007B` | `17FE007B` | `17FC007B` |
| Fahrzeug | 7 (29 Bit) | `17` | `FC0076` | `17FE0076` | `17FC0076` |
| DC/DC-Wandler | 7 (29 Bit) | `17` | `FC00B9` | `17FE00B9` | `17FC00B9` |
| Klima | **6 (11 Bit)** | `00` | `746` | `7B0` | `746` |
| Akku (Kapazität) | **6 (11 Bit)** | `00` | `710` | `77A` | `710` |

`0x746` und `0x710` passen in elf Bit, `0x17FC007B` nur in 29. Für die beiden
unteren Zeilen schaltet jolt kurz auf `ATSP6` um und im `finally` zurück.

### Flusskontrolle — der Handgriff, ohne den die Hälfte fehlt

```
ATFCSH<kopf>   ATFCSD300000   ATFCSM1
```

Passt eine Antwort nicht in einen CAN-Rahmen, muss der Fragende ein
Flow-Control-Paket zurücksenden. Der ELM327 macht das selbst — aber nur, wenn
er den Kopf kennt, und bei den MEB-Adressen rät er falsch. **Ohne diese drei
Befehle scheitert jede mehrteilige Antwort stumm.** Betroffen waren
Batteriestrom, Energiezähler, Reichweite und Kompressor — vier der
interessantesten Werte.

### Die Messwerte

`b[0]` ist das erste Byte **nach** der Quittung (`62` + Datenkennung), also
`g_dataBuffer[0]` bei codingABI und `B4` bei spot2000/WiCAN.

| Wert | DID | Gerät | Takt | Umrechnung | Anmerkung |
|---|---|---|---|---|---|
| Ladestand (roh) | `22028C` | BMS | jede | `b0/2,5` | Pflicht — ohne ihn wird die Runde verworfen |
| Spannung | `221E3B` | BMS | jede | `[b0:b1]/4` | ~377 V bei 79 % |
| **Strom** | `221E3D` | BMS | jede | `([b0:b3]−150000)/100` | **mehrteilig**; negativ = Entladung |
| **Entladen gesamt** | `221E32` | BMS | jede | `\|[b12:b15]\|/8583,07` | **mehrteilig, vorzeichenbehaftet** |
| Geladen gesamt | ↑ | BMS | jede | `[b8:b11]/8583,07` | aus derselben Antwort |
| Ladegrenze | `221E1B` | BMS | jede | `[b0:b1]/5` | |
| Betriebsart | `227448` | BMS | jede | `b0` | Bit 2 = lädt |
| Heizstrom (PTC) | `221620` | BMS | jede | `b0/4` | Zuheizer der Batterie |
| Tempo | `22F40D` | BMS | jede | `b0` | |
| Batterietemperatur | `222A0B` | BMS | 10 | `b0/2−40` | genauer als die Aussentemperatur für die Ladekurve |
| Nebenverbraucher | `220364` | Fahrzeug | jede | `[b0:b1]/10` | alles ausser dem Antrieb |
| **Kilometerstand** | `22295A` | Fahrzeug | jede | `[b0:b2]` | ganze km; korrigiert die GPS-Strecke |
| DC/DC-Strom | `22465B` | DC/DC | 10 | `[b0:b1]/16` | |
| Akkukapazität | `222AB2` | Akku | 40 | `[b0:b3]/1310,77/1000` | gemessen, nicht Prospekt |
| Reichweite | `222AB6` | Akku | 10 | `[b0:b1]` | **mehrteilig** |
| Aussentemperatur | `222609` | Klima | 20 | `b0/2−50` | |
| Innentemperatur | `222613` | Klima | 20 | `[b0:b1]/5−40` | |
| **Klimakompressor** | `220800` | Klima | 20 | `[b5:b6]` W | **mehrteilig**; `b0` Bit 0 = an, `[b3:b4]` = Drehzahl |

„Takt" ist die Rundenzahl: `jede` heisst jede Messung, `20` jede zwanzigste.
Selten gelesen wird, was sich langsam ändert oder einen Protokollwechsel
kostet.

### Wo die Quellen sich widersprechen

| Wert | jolt folgt | verworfen |
|---|---|---|
| **Strom** `221E3D` | spot2000 + codingABI: `([b0:b3]−150000)/100` | WiCAN: `(150000−[b1:b5])/100` — ergibt am Fahrzeug −383 731 A |
| **Reichweite** `222AB6` | codingABI: `[b0:b1]` → 297 km | WiCAN: `[b1:b2]` → 10 497 km |
| **Kapazität** `222AB2` | codingABI: vier Bytes | WiCAN: zwei Bytes × 50 — dieselbe Formel, gröber |

### Was keine Quelle wusste

Die **Kompressorleistung** steht in keiner der drei Listen; spot2000 führt
`220800` mit „equation missing". Eine Differenzmessung am Fahrzeug hat sie
entschieden — einmal mit und einmal ohne laufenden Kompressor:

```
          b0    b1b2   b3b4   b5b6   b7
aus     0x10       0      0      0    0
an      0x51    9408   9408   2618   14
Teillast        3648   3712    935    5
```

`b5b6` ist die Leistung in Watt (0 / 935 / 2618), `b1b2` und `b3b4` laufen
gleich und viel höher — Soll- und Ist-Drehzahl. Als Watt wären 9,4 kW für
einen Klimakompressor zu viel. Der Werkzeugkasten dafür steht unter
`/obd` → „Klimakompressor eingrenzen".

### Nicht implementiert

* `222AB8` **Energieinhalt** — mehrteilig, keine Quelle nennt eine Umrechnung.
  Für den Verbrauch braucht es ihn nicht: Die Differenz des Entladezählers ist
  genauer.
* Zellspannungen und -temperaturen (spot2000 führt über hundert davon) — für
  die Routenplanung ohne Belang.

### Zwei Ladestände

MEB-Fahrzeuge liefern **zwei**: den des Batteriemanagements und den der
Anzeige. `reserve_soc` und `ziel_soc` meinen den der Anzeige.

```
brutto = b0 / 2,5
Anzeige = brutto · 51/46 − 6,4        (auf 0…100 begrenzt)
```

Wer den falschen nimmt, rechnet dauerhaft um den verborgenen Puffer daneben —
bei 79 % Anzeige sind das gut zwei Prozentpunkte.

---

## Nächste Schritte

**Belegung der Ladepunkte.** Der Kommentar in `laden/verfuegbarkeit.py` sagt,
echte Belegungsdaten seien für ein Privatprojekt nicht zu haben. Das stimmt
nicht mehr — jedenfalls nicht für Deutschland. Die
[OCPDB von MobiData BW](https://mobidata-bw.de/dataset/e-ladesaulen) liefert
OCPI 3.0 **ohne Schlüssel und ohne Registrierung**, deutschlandweit, mit
Live-Status für über zehntausend Standorte. Nachgemessen bewegen sich rund
13 % der Ladepunkte pro Stunde — es sind echte Daten, keine Momentaufnahme.

Zwei Haken: Es gibt keine räumliche Filterung (`bbox` wird stillschweigend
ignoriert), man muss den Bestand also periodisch synchronisieren statt pro
Anfrage abzufragen. Und für **Frankreich** gibt es nichts — der nationale
Zugangspunkt führt unter „temps réel" null IRVE-Datensätze.

Die vorbereitete `VerfuegbarkeitsQuelle`-Schnittstelle passt: ein Adapter, der
`Zustand(frei=…, quelle="ocpi")` liefert statt `Unbekannt`. Der Aufwand liegt
weniger im OCPI-Teil als im Abgleich mit dem eigenen Ladepunkt-Bestand — die
Live-Einträge tragen eine `evse_id`, jolts OCM-Import speichert die nicht.

**Wichtig dabei:** `redundanz_bonus` bleibt. Live-Daten gibt es für etwa ein
Zehntel der Standorte und für Frankreich gar nicht; ein Optimierer, der
Standorte ohne Live-Daten benachteiligt, wählt auf einer Frankreichfahrt
systematisch die falschen.

**Messpunkte puffern.** Was während eines Funklochs nicht rausgeht, ist heute
verloren — `positionMelden` schluckt den fehlgeschlagenen POST stillschweigend,
und einen Puffer gibt es nirgends. Für eine geplante Fahrt ist das harmlos, der
nächste Punkt kommt. Für eine **Aufzeichnung** ist es teuer: Gemessen an einer
echten Strecke fehlen nach zwanzig Minuten ohne Netz bis zu 13 % der Strecke,
und der gelernte Faktor verschiebt sich um bis zu 35 % — unsichtbar, als stille
Verschiebung in einer Zahl, die dauerhaft im Fahrzeug bleibt. Der
Kilometerstand fängt inzwischen die Strecke wieder ein, nicht aber den Verlauf.

Das ist **nicht rein im Frontend** zu machen: Das Eingabemodell `Messpunkt` hat
kein `zeit`-Feld, nachgereichte Punkte bekämen alle den Zeitstempel des
Nachreichens — und damit wäre der Zeitfaktor kaputt statt der Strecke.
`messpunkt_aufnehmen` kann `zeit` bereits, es fehlt nur der Weg durch die
Schnittstelle.

**Höchstgeschwindigkeit am Fahrzeug.** Der Tempo-Regler hat keine absolute
Obergrenze; bei 130 % rechnet das Modell mit 165 km/h, die kein Serienfahrzeug
mit diesem Luftwiderstand fährt. Für ein **Gespann** ist 100 km/h ausserdem
keine Vorliebe, sondern eine harte Grenze — der Regler bildet das falsch ab.
Sauber wäre eine Höchstgeschwindigkeit am Fahrzeug und ein Anhänger als eigene
Grösse (Masse plus Luftwiderstands-Aufschlag), statt den cw-Wert des Autos zu
verbiegen.

**Was der Kompressor sonst noch hergibt.** `220800` liefert neben der Leistung
Soll- und Ist-Drehzahl; ein Wert steht noch ohne Deutung (`b7`, dieselbe Grösse
wie die Leistung im Verhältnis 1:187). Und `222AB8` (Energieinhalt) wartet
weiter auf eine Umrechnung — gebraucht wird er nicht, die Differenz des
Entladezählers ist genauer.
