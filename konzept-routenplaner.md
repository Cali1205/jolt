# jolt — Konzept

Ein Routenplaner für Elektroautos, dessen eigentliche Aufgabe nicht das Planen
ist, sondern das **Nachführen**.

---

## 1. Das Problem

Jeder Ladeplaner rechnet beim Losfahren einen Plan: hier laden, so lange, dann
weiter. Der Plan ist zu diesem Zeitpunkt korrekt. Nach achtzig Kilometern ist er
es nicht mehr.

Dafür genügen Kleinigkeiten, die sich alle in dieselbe Richtung addieren:

- Man fährt 135 statt der angenommenen 120 km/h.
- Es sind 2 °C statt der 15 °C, mit denen die Herstellerangabe entstand.
- Gegenwind mit 25 km/h.
- Die Heizung läuft, die Batterie ist kalt und nimmt am Schnelllader nicht die
  versprochenen 150 kW.
- Der Rasthof, der im Plan steht, hat vier Ladepunkte und alle sind belegt.

Jeder dieser Punkte kostet für sich genommen wenige Prozent. Zusammen
verschieben sie den Ankunfts-SoC am nächsten Stopp um zehn bis zwanzig
Prozentpunkte — und das ist genau die Größenordnung, in der aus „entspannt
ankommen" ein Schleichen mit 90 km/h auf dem Standstreifen wird.

Der übliche Umgang damit ist ein großzügiger Puffer: Man plant mit 20 % Restladung
statt 8 %. Das funktioniert, kostet aber bei jeder Fahrt Zeit — der Puffer wird
an der Säule bezahlt, im steilsten Teil der Ladekurve zwar nicht, aber die
zusätzlichen Stopps summieren sich.

**Die These von jolt:** Wer den Plan während der Fahrt an der Wirklichkeit
nachzieht, braucht den Puffer nicht. Nicht der bessere Startplan ist die Lösung,
sondern der Plan, der merkt, dass er falsch geworden ist.

---

## 2. Was jolt anders macht

### 2.1 Verbrauch wird gerechnet, nicht geschätzt

Fast alle Planer arbeiten mit einem pauschalen Verbrauch in kWh/100 km, oft mit
einem Schieberegler für „Fahrstil". Das ist der Grund, warum sie im Winter und
in den Bergen danebenliegen: Ein Pauschalwert kann nicht wissen, dass die
nächsten 40 km 900 Höhenmeter bergauf führen.

jolt zerlegt die Route in Segmente und rechnet je Segment die Physik:

```
F_roll  = c_rr · m · g · cos(θ)
F_luft  = ½ · ρ(T, h) · c_w · A · (v + v_gegen)²
F_steig = m · g · sin(θ)

E_segment = (F_roll + F_luft + F_steig) · s / η_antrieb      wenn > 0
E_segment = (F_roll + F_luft + F_steig) · s · η_rekup        wenn < 0
E_neben   = P_hvac(T_außen) · t_segment
```

Drei Dinge, die daran wichtig sind und die ein Pauschalwert nicht leisten kann:

**Das `v²` beim Luftwiderstand.** Der Unterschied zwischen 110 und 130 km/h ist
nicht 18 % mehr Luftwiderstand, sondern 40 %. Weil der Luftwiderstand auf der
Autobahn den größten Anteil am Verbrauch hat, ist die Tempowahl der stärkste
Hebel, den der Fahrer hat — und der einzige, den er *während* der Fahrt noch
betätigen kann. Ein Planer, der das nicht abbildet, kann auf die Frage „schaffe
ich es noch, wenn ich 110 fahre?" nicht antworten. Genau diese Frage stellt man
sich aber bei 12 % Restladung.

**Die Höhe.** `sin(θ)` ist bei 5 % Steigung nur 0,05 — aber bei 1,5 t Fahrzeug
sind das 735 N zusätzliche Kraft, mehr als Roll- und Luftwiderstand zusammen.
Bergauf verbraucht ein E-Auto dramatisch mehr; bergab holt es einen Teil über
Rekuperation zurück, aber eben nur einen Teil (`η_rekup` ≈ 0,7). Über einen Pass
ist die Bilanz deutlich negativ, obwohl man am Ende wieder auf Ausgangshöhe ist.
Deshalb braucht das Modell ein echtes Höhenprofil und nicht nur eine Distanz.

**Die Nebenverbraucher.** `P_hvac` ist bei 20 °C fast null und bei −5 °C zwischen
2 und 4 kW. Entscheidend ist, dass diese Leistung an der **Zeit** hängt, nicht an
der Strecke: Im Stau kostet die Heizung genauso viel wie bei 130 km/h, nur ohne
zurückgelegte Kilometer. Das ist der Grund, warum Winterfahrten mit Stau die
Prognose am härtesten treffen.

### 2.2 Das Modell lernt das eigene Auto

Die Physik oben braucht Fahrzeugparameter — `c_w`, Stirnfläche, Rollwiderstand,
Wirkungsgrad. Die kennt niemand genau, und sie ändern sich mit Reifen, Dachbox,
Beladung und Alter der Batterie.

Deshalb hat jedes Fahrzeug einen **Korrekturfaktor**, der aus echten Fahrten
gewonnen wird: prognostizierte kWh gegen tatsächlich verbrauchte kWh. Nach
einigen Fahrten kennt jolt das konkrete Auto besser als jede Datenbank — inklusive
der Dachbox, die seit Ostern oben ist.

Genau hier docken die OBD2-Logger an: Sie liefern die
Ist-Werte, aus denen der Faktor entsteht.

### 2.3 Der Plan wird während der Fahrt nachgezogen

Das ist die Live-Funktion, und sie ist der Grund für dieses Projekt.

Während der Fahrt kommen laufend Messpunkte herein: Position, SoC, Tempo,
Außentemperatur. jolt vergleicht daraus fortwährend zwei Zahlen:

- **Soll-SoC** — was der Plan an dieser Stelle vorhergesagt hat.
- **Ist-SoC** — was das Auto meldet.

Aus der Abweichung über die letzten Kilometer entsteht ein laufender
Verbrauchsfaktor, mit dem der Rest der Strecke neu gerechnet wird. Neu geplant
wird nicht bei jeder Messung, sondern wenn einer dieser Auslöser greift:

| Auslöser | Schwelle |
|---|---|
| Prognostizierter Ankunfts-SoC am nächsten Stopp weicht ab | > 5 Prozentpunkte |
| Prognose fällt unter die Reserve | sofort |
| Nächster Ladepunkt meldet sich als belegt oder defekt | sofort |
| Fahrzeug verlässt die geplante Route | > 500 m für > 1 min |
| Ankunftszeit verschiebt sich (Stau) | > 10 min |

Der Grund für Schwellen statt „jedes Mal": Ein Plan, der sich alle 30 Sekunden
ändert, ist kein Plan. Wer gerade beschlossen hat, in 40 km Pause zu machen,
soll das nicht dreimal umwerfen müssen. Eine Änderung muss etwas bedeuten.

### 2.4 Die Ladekurve ist keine Zahl

„150 kW Ladeleistung" ist eine Spitzenangabe, die zwischen 20 und 40 % SoC gilt.
Bei 70 % sind es vielleicht noch 60 kW, bei 85 % noch 35. Wer den Unterschied
ignoriert, plant zu wenige, zu lange Stopps.

jolt hinterlegt je Fahrzeug eine Kurve als Stützstellen `(SoC %, kW)` und
interpoliert dazwischen. Die tatsächliche Leistung ist dann

```
P = min( Kurve(SoC), P_max_Ladepunkt, P_max_Fahrzeug ) · f_temperatur
```

Daraus folgt eine Regel, die den Zeitgewinn bringt: **Lieber zweimal kurz von
10 auf 55 % als einmal lang von 10 auf 90 %.** Die letzten 30 Prozentpunkte
kosten oft mehr Zeit als die ersten sechzig. Der Optimierer (Abschnitt 4) nutzt
das aus; als Nutzer sieht man nur, dass die Stopps kürzer sind als erwartet.

---

## 3. Woher die Daten kommen

| Zweck | Quelle | Anmerkung |
|---|---|---|
| Route + Höhenprofil | openrouteservice | 2 500 Anfragen/Tag kostenlos, `elevation=true` liefert Höhe pro Stützpunkt |
| Ladesäulen Deutschland | Bundesnetzagentur-Ladesäulenregister (CSV) | amtlich, vollständig, ohne Schlüssel |
| Ladesäulen international | Open Charge Map | freier API-Schlüssel, 300 000+ Ladepunkte |
| Temperatur und Wind | Open-Meteo | ohne Schlüssel |
| Live-SoC | zunächst manuell / Simulator | vorbereitet für OBD2-Logger und Hersteller-APIs |

Das Routing liegt hinter einem schmalen Interface (`RoutingProvider`). Ein
selbstgehostetes **Valhalla** ist damit später nur ein zweiter Adapter, kein
Umbau — relevant, sobald das Tageskontingent von 2 500 Anfragen eng wird oder
die Live-Neuplanung häufiger rechnet.

### Verfügbarkeit ist das ungelöste Problem

Echte Belegungsdaten öffentlicher Ladesäulen sind in Deutschland nicht frei
verfügbar. Wer sie hat, hat sie über **OCPI**-Verträge mit Betreibern oder über
kommerzielle Aggregatoren. Das ist für ein Privatprojekt vorerst verschlossen.

jolt geht deshalb ehrlich damit um, statt Verfügbarkeit vorzutäuschen:

1. `Verfuegbarkeit` ist ein Interface. Eine OCPI-Anbindung ist später ein
   Adapter, kein Umbau.
2. Solange keine Daten da sind, zählt **Redundanz**: Ein Standort mit acht
   Ladepunkten wird einem mit zwei vorgezogen, auch wenn er zwei Minuten Umweg
   kostet. Das ist die beste verfügbare Näherung an „da ist wahrscheinlich was
   frei".
3. Der Nutzer kann in der App melden, dass ein Standort belegt ist. Das gilt
   für die laufende Fahrt und löst sofort eine Neuplanung aus.
4. Zu jedem Stopp wird ein **Ausweichstandort** mitgeplant, der ohne Nachladen
   erreichbar bleibt. Wenn vor Ort alles belegt ist, muss niemand neu suchen.

---

## 4. Die Ladestopp-Planung

*(Implementiert in `backend/app/laden/optimierer.py`, geprüft von
`tools/check_optimierer.py`.)*

Die Aufgabe: Finde die Folge von Ladestopps und Lademengen, die die
**Gesamtreisezeit** minimiert, unter der Nebenbedingung, dass der SoC nie unter
die Reserve fällt und am Ziel der gewünschte Ziel-SoC erreicht ist.

Das ist kein kürzester Weg, sondern ein kürzester Weg mit einer kontinuierlichen
Entscheidungsvariablen je Knoten (wie viel wird geladen). Der Weg dahin:

**Schritt 1 — Kandidaten.** Alle Ladepunkte im Korridor um die Route, gefiltert
nach Steckertyp und Mindestleistung. Je Kandidat der Umweg in Minuten
(Abfahrt + Zufahrt + Rückweg). Kandidaten mit mehr als ~10 min Umweg fallen
raus; sie gewinnen die Zeit an der Säule fast nie zurück.

**Schritt 2 — Graph.** Knoten = Start, Kandidaten (geordnet nach Fortschritt
entlang der Route), Ziel. Eine Kante `i → j` existiert, wenn die Etappe mit
voller nutzbarer Batterie überhaupt fahrbar ist. Kantenkosten = Fahrzeit +
Umwegzeit; der Energiebedarf der Etappe kommt aus dem Verbrauchsmodell.

**Schritt 3 — Suche.** Dijkstra über den Zustand `(Ladepunkt, Ankunfts-SoC)`.
Weil der SoC kontinuierlich ist, wird je Knoten eine **Pareto-Front** von
Labels `(Zeit, SoC)` geführt: Ein Label wird verworfen, wenn ein anderes
gleichzeitig früher *und* mit mehr Ladung dort ist. Das hält die Zustandsmenge
klein, ohne den SoC grob zu diskretisieren.

Die Ladezeit an einem Knoten folgt aus der Ladekurve:

```
t_laden(SoC_an → SoC_ab) = ∫ (E_akku / P(s)) ds
```

**Schritt 4 — Nachoptimierung.** Die Lösung wird lokal verschoben: Ladehübe
wandern in den steilen Teil der Kurve (grob 10–60 %), soweit die Reserve das
zulässt. Typischerweise werden dadurch Stopps kürzer und manchmal einer mehr —
in Summe schneller.

**Schritt 5 — Ausweichstandorte.** Zu jedem Stopp wird der beste Alternativstopp
bestimmt, der ohne Nachladen noch erreichbar ist.

### Warum nicht einfach gierig?

Ein gieriger Planer („fahr, bis die Reserve erreicht ist, lade dort, wo du gerade
bist") ist einfach und in der Ebene brauchbar. Er scheitert an zwei Stellen
systematisch: vor langen Lücken ohne Schnelllader, wo man *vorher* mehr hätte
laden müssen, und bei der Wahl zwischen einem 50-kW- und einem 300-kW-Standort
zwanzig Kilometer später. Beides sind genau die Fälle, in denen ein Planer sich
lohnt — deshalb der Aufwand mit der Pareto-Front.

---

## 5. Aufbau

Bewusst dieselben Konventionen wie `nest`: FastAPI + SQLAlchemy + Alembic,
PostgreSQL im Docker mit SQLite-Fallback für die lokale Entwicklung, eine
Vanilla-JS-PWA ohne Build-Schritt, deutschsprachige Kommentare, die das *Warum*
festhalten.

```
backend/app/
  routing/    provider.py (Interface) · ors.py · korridor.py
  energie/    modell.py · wetter.py · kalibrierung.py
  laden/      kurven.py · saeulen_import.py · verfuegbarkeit.py
  live/       sitzung.py · kanal.py (WebSocket) · simulator.py
  routers/    auth · fahrzeuge · route · saeulen · live
frontend/     index.html · karte.js · route.js · fahrzeug.js · live.js
tools/        import_bnetza.py · import_ocm.py · check_modell.py · check_backend.py
```

**Kein PostGIS.** Der einzige Geo-Query, den jolt braucht, ist „alle Ladepunkte
im Korridor um eine Polyline". Das löst ein Index auf `(lat, lon)` mit
Bounding-Box-Vorfilter und anschließender Haversine-Rechnung bei rund 150 000
deutschen Ladepunkten in Millisekunden — und erhält den SQLite-Fallback, der die
lokale Entwicklung ohne laufenden Postgres möglich macht. PostGIS bleibt die
Option, sobald Isochronen dazukommen.

---

## 6. Stufen

**Stufe 1 — was jetzt da ist**

- Fahrzeugprofile mit Ladekurve, inklusive Vorlagen gängiger Modelle
- Ladesäulen-Import von Bundesnetzagentur und Open Charge Map, idempotent
- Route mit Höhenprofil, Wetter entlang der Strecke
- Das Verbrauchsmodell, vollständig — inklusive Reichweitenmarke auf der Karte:
  der Punkt, an dem der SoC die Reserve erreicht
- Ladepunkte im Korridor mit Umwegzeit
- Live-Gerüst: Messpunkt-Endpunkt, WebSocket, Fahrt-Simulator, Ist gegen Soll
  in der PWA

**Stufe 2 — der Optimierer** *(steht)*

Abschnitt 4 in Code: Kandidatengraph, Pareto-Dijkstra, Nachoptimierung,
Ausweichstandorte. Als `POST /api/fahrten/{id}/ladeplan` und als Ladeplan in
der PWA.

Zwei Dinge sind dabei anders gekommen als geplant:

- Die Etappenprüfung schaut nicht auf die Bilanz am Etappenende, sondern auf
  den **grössten kumulierten Bedarf innerhalb der Etappe**. Über einen Pass
  sieht die Bilanz am Ende harmlos aus, weil die Rekuperation auf der Abfahrt
  einen Teil zurückholt — oben wäre der Akku trotzdem leer. Ohne diese
  Unterscheidung plant der Optimierer Etappen, die in der Mitte nicht machbar
  sind.
- Der Ausweichstandort darf **in die Reserve hineingehen**, bis zur Hälfte.
  Der Plan selbst rührt sie nie an; er kommt überall mit mindestens
  `reserve_soc` an. Ein Ausweichstandort, der die volle Reserve stehen lassen
  muss, wäre deshalb fast nie erreichbar — und die Reserve ist genau für
  diesen Fall da. Gibt es keinen, sagt der Plan das, statt die Lücke zu
  verschweigen.

**Stufe 3 — die Live-Neuplanung** *(steht)*

Die Auslöser aus 2.3 vollständig, in `live/sitzung.py`; die Umplanung selbst in
`live/umplanung.py`. Neu geplant wird die **Reststrecke** ab der aktuellen
Position mit dem aktuellen Ladestand — für den Optimierer aus Stufe 2 ist das
dieselbe Aufgabe wie vor der Abfahrt, nur mit besseren Zahlen.

Drei Dinge sind dabei dazugekommen, die im Konzept so nicht standen:

- Ein zweiter Faktor für die **Zeit**. Der Verbrauchsfaktor sieht einen Stau
  nicht: Wer steht, verbraucht je Kilometer sogar etwas mehr, aber die
  Ankunftszeit verschiebt sich um ein Vielfaches davon. Ohne eigene Zahl wäre
  der Auslöser „Ankunftszeit verschiebt sich" nicht zu haben — und jede
  Ankunftszeit im umgeplanten Ladeplan wäre die aus dem alten Plan.
- Eine **Sperre** gegen zu häufiges Umplanen. Die Schwellen aus 2.3 sagen,
  *wann* etwas nicht mehr stimmt — sie sagen nicht, wann es wieder stimmt. Eine
  Abweichung von acht Prozentpunkten besteht bei der nächsten Messung immer
  noch, und bei der übernächsten auch. Ohne Sperre rechnete deshalb jede
  einzelne Messung neu. Dringende Gründe (Säule belegt, Reserve reicht nicht)
  gehen immer durch, alles andere erst wieder nach zehn Kilometern.
- Der Simulator bekam eine **simulierte Uhr**. Er spielt Stunden in Sekunden
  ab; gegen die echte Uhr gemessen wäre jeder Zeitfaktor Unsinn. Mit
  simulierten Zeitstempeln lässt sich stattdessen ein Stau durchspielen — und
  damit genau der Auslöser prüfen, den der Verbrauch nie auslöst.

Dazu **Web Push** (`push.py`): Eine Planänderung erreicht das Telefon auch mit
dunklem Bildschirm, weil der Service Worker sie entgegennimmt, wenn die Seite
längst geschlossen ist. Ohne VAPID-Schlüssel ist die Funktion aus — dieselbe
Haltung wie bei `ORS_API_KEY` und `APP_PASSWORT`: Was nicht eingerichtet ist,
wird nicht vorgetäuscht.

Eine Entscheidung, die dabei zählt: **Ein totes Abo wird gelöscht, ein
gestörtes nicht.** Ein Browser, der die Erlaubnis entzogen hat, antwortet mit
404 oder 410; das Abo ist dann endgültig wertlos. Eine 500 des Push-Dienstes
sagt dagegen nichts über das Abo aus — wer es dabei wegwirft, schaltet die
Benachrichtigungen bei der ersten Störung dauerhaft ab, und niemand merkt,
warum sie nicht mehr kommen.

**Stufe 4 — echte Fahrzeugdaten** *(steht)*

Anbindung der OBD2-Logger bzw. einer Hersteller-API. Das Datenmodell
(`LiveSitzung` / `LivePunkt`) nimmt sie unverändert entgegen — an *dieser*
Stelle ändert sich tatsächlich nichts.

Der Weg dorthin brauchte aber sehr wohl eine Ergänzung, und zwar eine, die
beim Entwurf übersehen wurde: Messpunkte kamen ausschliesslich über
`/api/live/{sitzung_id}/punkt` herein. Das passt zur PWA, die die Fahrt selbst
gestartet hat und die ID deshalb kennt — aber nicht zu einem Gerät, das im
Auto verbaut ist und beim Anschalten einfach zu senden beginnt. Die
Sitzungs-ID entsteht erst beim Losfahren und wechselt mit jeder Fahrt; ein
Dongle kann sie nicht wissen. Deshalb trägt jetzt das *Fahrzeug* ein
langlebiges Logger-Token, und `POST /api/live/melden` sucht dessen laufende
Sitzung selbst.

Ein ELM327 liest den Ladestand eines MEB-Fahrzeugs nicht über die genormten
OBD2-PIDs — die sind auf Verbrennungsmotoren gemünzt —, sondern über
herstellerspezifische UDS-Abfragen. Der Ladestand steht auf Service `0x22`,
DID `028C`, Rohwert durch 2,5.

Die Adresse dagegen ist **nicht** der naheliegende 11-Bit-Header `7E5`,
sondern eine 29-Bit-Kennung: `0x17FC007B` fragen, `0x17FE007B` antwortet, und
beim ELM327 heisst das `ATSP7` · `ATCP17` · `ATSHFC007B` · `ATCRA17FE007B`.
Bis das feststand, kam auf jede Abfrage `NO DATA` — die Vermutung mit dem
`7E5` hat mehrere Anläufe gekostet. Welche Werte es sonst noch gibt und
welche Fallstricke dazugehören, steht in der README unter „Was das Auto
hergibt".

Dafür steht `live/quellen/` analog zu `routing/provider.py`: eine Datei je
Format, die auf einen `Rohpunkt` normalisiert. Mitgeliefert sind jolts
eigenes Format und das von Iternio (ABRP), weil letzteres im Umfeld der
Elektroauto-Logger ein Quasi-Standard ist. **Die Übersetzung kennt kein
Netz** — sie bekommt ein geparstes Objekt und gibt einen `Rohpunkt` zurück.
Ob der aus einem POST kam, aus einer Abfrage bei einem fremden Dienst oder
aus einer Datei, ist eine Frage des Transports. Genau deshalb lässt sich ein
neues Format anhand einer aufgezeichneten Antwort einbauen, ohne im Auto zu
sitzen, und `check_quellen.py` läuft ohne alles.

**Den Transport gibt es inzwischen, und er kommt ohne fremde Cloud aus.**

Erwogen war die ABRP-App als Sensortreiber plus deren Telemetrie-API — für
ein iPhone schien das der einzige Weg, denn Car Scanner exportiert dort nur
Aufzeichnungsdateien. Ein Umweg über einen fremden Dienst wäre es trotzdem
gewesen, und die Alternative — ein ESP32-Dongle mit eigener Verbindung —
hätte kein GPS gehabt.

Gebaut wurde ein dritter Weg: **Web Bluetooth**, direkt aus der PWA. Der
Browser spricht selbst mit dem BLE-Dongle, das Telefon liefert die Position
dazu, und niemand sonst sieht die Daten. Auf iOS braucht es dafür den Browser
**Bluefy** — Safari kennt Web Bluetooth nicht —, und `requestDevice` verlangt
zwingend eine Nutzergeste. Beides sind Einschränkungen, aber keine, die einen
fremden Dienst nötig machen.

Was dabei nicht vorgesehen war und doch entscheidend wurde: Das Fahrzeug
spricht auf **zwei Rahmenbreiten** (Klima und Akku auf 11 Bit, alles andere
auf 29), und ohne die drei Flusskontroll-Befehle scheitert jede Antwort, die
nicht in einen CAN-Rahmen passt — stumm. Vier der interessantesten Werte
fehlten deshalb monatelang, ohne dass irgendwo stand, dass sie fehlen.

`live/quellen/` bleibt trotzdem richtig: Ein Logger, der ein fremdes Format
spricht, meldet weiterhin über `POST /api/live/melden` — der Weg über Web
Bluetooth ist eine Quelle mehr, nicht die einzige.

**Stufe 5 — Kalibrierung aus echten Fahrten** *(steht)*

Der Korrekturfaktor aus 2.2, gefüttert aus abgeschlossenen Fahrten. Er wird
beim Beenden einer Live-Sitzung fortgeschrieben, gedämpft und nur, wenn die
Fahrt lang genug und der gemessene Faktor plausibel war.

Was dabei erst der Praxistest zeigte: **Ladeabschnitte müssen heraus.** Die
erste Fassung nahm Ladestand am Anfang minus am Ende — wer unterwegs vierzig
Prozentpunkte nachlädt, sieht damit einen Verlust, der um vierzig zu klein
ist. Der gelernte Faktor fiel entsprechend zu niedrig aus, und weil er in den
Plausibilitätsgrenzen blieb, fiel es nicht auf. Aus einer Fahrt mit
29 kWh/100 km lernte das Fahrzeug 12.

**Stufe 6 — messen statt schätzen** *(steht)*

Nicht vorgesehen, aber aus der ersten echten Aufzeichnung erzwungen: An zu
vielen Stellen stand eine Schätzung, wo das Fahrzeug die Zahl selbst kennt.

- **Strecke** aus dem Kilometerstand statt aus dem GPS. Bei einer Grösse mit
  der Strecke im Nenner ist das der Unterschied zwischen brauchbar und
  irreführend.
- **Energie** aus den Lebensdauerzählern statt aus dem Ladestand. Ein
  Ladestandsschritt sind 339 Wh, ein Zählerschritt 0,117 — fast
  dreitausendmal feiner. Erst damit ist ein Verbrauchsbalken je Minute eine
  Messung statt Rauschen.
- **Akkukapazität** vom Fahrzeug statt aus dem Prospekt. Beim ID.Buzz 73,8
  statt 77 kWh nach 60 000 km; an dieser Zahl hängt jede Umrechnung zwischen
  Prozent und Kilowattstunden.

Der Grundsatz dahinter gilt über den OBD2-Teil hinaus: Wo eine gemessene und
eine angenommene Zahl zur Wahl stehen, gewinnt die gemessene — und wo keine
gemessene da ist, soll die Oberfläche es sagen, statt die Annahme wie eine
Messung aussehen zu lassen.

---

## 7. Was jolt bewusst nicht wird

- **Kein Navigationsgerät.** Die Abbiegehinweise macht das Telefon oder das Auto.
  jolt beantwortet, *wo und wie lange* geladen wird — der Rest ist gelöst.
- **Keine Bezahlfunktion.** Ladekarten und Roaming sind ein eigenes Geschäft.
- **Keine Fremdnutzer.** Selbstgehostet, für die eigenen Fahrzeuge. Das erlaubt
  einfache Auth und In-Memory-Zustand statt einer Nutzerverwaltung.
