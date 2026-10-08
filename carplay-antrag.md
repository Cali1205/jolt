# CarPlay-Antrag für jolt (Entwurf)

Schritt 8 aus [`konzept-ios-app.md`](konzept-ios-app.md). Der Antrag gilt **Weg B**
(eigene CarPlay-App mit Vorlagen). Weg A, die Live Activity im CarPlay-Dashboard,
braucht keinen Antrag und läuft schon.

Quelle für Kategorien und Regeln: Apples *CarPlay Developer Guide* (Stand
8.6.2026). Was dort nicht steht, ist unten als **offen** gekennzeichnet.

## Was du tun musst

1. Auf <https://developer.apple.com/carplay/> den Antrag stellen (Abschnitt
   „Request CarPlay entitlement"). Es braucht das Konto, mit dem die App in
   App Store Connect liegt; die Team-ID steht in `APPLE_TEAM_ID`.
2. Die Kategorie **EV charging** wählen (Entitlement
   `com.apple.developer.carplay-charging`). Rückfall: **Driving task**.
3. Den englischen Text unten in das Beschreibungsfeld einfügen. Ich habe nur
   aufgeschrieben, was jolt heute kann oder was in diesem Antrag als Plan
   ausdrücklich so benannt ist.
4. Apple prüft und ordnet dem Konto das Entitlement zu. Danach braucht das
   Provisionierungsprofil die CarPlay-Fähigkeit; mit der automatischen
   Signatur (`-allowProvisioningUpdates`) legt Apple das beim Export an, das
   ist aber **ungeprüft**.

## Entscheidung: EV charging oder Driving task

| | EV charging | Driving task |
|---|---|---|
| Passt zu jolt | Der Kern ist die Planung von Ladestopps | Nur „Aufgaben, die bei der Fahrt helfen" |
| Tiefe der Vorlagen | 5 Ebenen | 2, ab iOS 26.4: 3 |
| Aktualisierung | keine Grenze genannt | höchstens alle 10 s |
| Bedingung | App muss mehr leisten als eine Liste von Ladesäulen; auf Karten nur Ladesäulen | keine Karte, keine Ortssuche |
| Risiko | Apple lehnt ab, wenn es nach einer reinen Ladesäulenliste aussieht | enger, aber unkritisch |

Empfehlung: **EV charging.** jolt plant die Route, rechnet den Ladestand und wählt
die Ladestopps; die Ladesäulen sind das Ergebnis, nicht der Inhalt. Das ist genau
die Abgrenzung, die der Leitfaden verlangt.

## Was in CarPlay erscheinen soll (und was nicht)

Alle Vorlagen sind feste Vorlagen von iOS; jolt zeichnet nichts selbst.

| Bildschirm | Vorlage | Inhalt |
|---|---|---|
| Start | Information | Ladestand, nächster Ladestopp (Name, Entfernung, erwarteter Ladestand bei Ankunft), Ankunftsverschiebung gegenüber dem Plan |
| Stopps | Liste | Die geplanten Ladestopps der laufenden Fahrt, mit Entfernung und Ladestand bei Ankunft |
| Stopp | Information | Betreiber, Leistung, Anschlüsse, Ladezeit laut Plan |
| Ausweichen | Liste / Point of Interest | Alternativen in der Nähe, wenn der geplante Stopp nicht erreichbar ist |

**Nicht** in CarPlay: Planung einer neuen Route (das passiert vor der Fahrt auf
dem iPhone), Einstellungen, Fahrzeugdaten, Diagramme, Konto.

**Offen:** Apple verlangt, dass „jeder Ablauf ohne iPhone möglich" ist. Heute wird
die Fahrt auf dem iPhone geplant oder die Aufzeichnung dort gestartet. Damit
CarPlay allein genügt, bräuchte es in CarPlay eine **Auswahl gespeicherter
Fahrten** (Liste). Das ist nicht gebaut und gehört in Schritt 10; im Antrag
steht es als Plan.

## Englischer Text für das Antragsformular

> **App name:** jolt
>
> **Category requested:** EV charging
>
> **What the app does.** jolt is an electric-vehicle trip planner. The user picks
> a vehicle and a destination; jolt calculates the route with elevation, models
> the vehicle's energy consumption (weight, drag, rolling resistance,
> temperature, traffic), and selects the charging stops that get the car
> to the destination with a safe reserve. It is not a directory of charging
> stations: stations are chosen as the result of the plan, based on the
> predicted state of charge at each point of the route.
>
> **During the drive.** The app records the trip, compares the planned and the
> actual energy use (the vehicle's state of charge is read from an OBD-II
> adapter over Bluetooth Low Energy), and re-plans the charging stops when the
> consumption deviates from the prediction or the arrival time shifts.
>
> **What CarPlay would show.** A short, read-and-glance status using only
> system templates:
> 1. an Information template with the state of charge, the next charging stop
>    (name, distance, expected state of charge on arrival) and the arrival time
>    versus plan;
> 2. a List template with the planned charging stops of the current trip;
> 3. an Information template for a single stop (operator, power, connectors,
>    planned charging time);
> 4. a List or Point of Interest template with nearby alternatives when the
>    planned stop cannot be reached.
>
> We plan to add a List template of saved trips so that a drive can be started
> and followed entirely from CarPlay without handling the iPhone.
>
> **What CarPlay would not do.** It does not show a map, does not take text
> input, does not ask the driver to pick up the iPhone, and shows nothing
> unrelated to the trip (no settings, no account screens).
>
> **Data and updates.** The data comes from our own server; the app forwards the
> latest state of the trip to the templates at a low rate (at most one update
> every few seconds), well within the limits of the category.
>
> **Distribution.** The app is currently in TestFlight and used by a single
> household. We would like to develop and test the CarPlay interface against
> real vehicles under this entitlement.
>
> **Contact:** r.schillinger@gmail.com

## Was ich nicht wissen kann

- Ob Apple eine App akzeptiert, die noch nicht im App Store ist und von einem
  einzelnen Haushalt genutzt wird. Der Leitfaden nennt dafür keine Bedingung, aber
  die Prüfung ist Apples Entscheidung. Der Absatz „Distribution" ist deshalb
  ehrlich gehalten und nicht geschönt.
- Ob ein späterer Wechsel der Kategorie einen neuen Antrag braucht
  (Annahme: ja).
- Wie lange die Prüfung dauert.

## Wenn der Antrag durch ist

1. Entitlement `com.apple.developer.carplay-charging` im Provisionierungsprofil.
2. CarPlay-Szene (Szenen-Delegate) im Plugin, Szenen-Manifest über
   `tools/ios_info_plist.sh`, Entitlement per `CODE_SIGN_ENTITLEMENTS`.
3. Die Vorlagen füllt dasselbe Anzeigemodell (`frontend/display.js`), das schon
   die Live Activity speist.
4. Prüfen: im CarPlay Simulator (Mac) oder im Fahrzeug über TestFlight.
