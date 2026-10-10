# jolt — route planner for electric cars

Plans charging stops and **updates the plan while you drive**. That is the
point: a plan calculated at departure is wrong after eighty kilometres — speed,
temperature, wind and traffic all add up in the same direction. Anyone who
notices that does not need a twenty percent safety buffer.

**Stack:** FastAPI + PostgreSQL (Docker, local SQLite fallback) · vanilla-JS PWA
with no build step · openrouteservice for routing with elevation profile ·
Bundesnetzagentur and Open Charge Map for the charge points · Open-Meteo for the weather

The detailed concept with the reasoning behind every decision is in
**[konzept-routenplaner.md](konzept-routenplaner.md)** (in German).

---

## What jolt can do today

- **Calculate consumption physically** instead of a flat kWh/100 km — air
  drag with v², gradient from the elevation profile, regeneration with realistic
  efficiency, heating by time rather than by distance. The difference between
  110 and 130 km/h is 40 % more air drag, not 18; a planner has to be able to
  model that, otherwise it is cosmetics.
- **Vehicle profiles with charging curve** — including templates, so nobody has
  to guess a drag coefficient (cw) on day one.
- **Import chargers** from the official register of the Bundesnetzagentur
  and from Open Charge Map. Both imports are idempotent.
- **Range marker on the map** — the point where the state of charge reaches the
  reserve. Even without charging-stop planning, this is the answer to the
  question that matters before departure.
- **Charge points in the corridor** with detour time in minutes, sorted by
  progress along the route.
- **Your own trips as route candidates.** Geometrically generated detours are
  worthless (all twelve lost on the test route); the route that won came
  from two points of the **driven** track. If an earlier trip fits start
  and destination — forwards, backwards or as a section, recorded or planned
  and driven — jolt picks waypoints along the driven path (every
  25 km, at most 20, never at breaks and charging sites that would force a
  detour as a waypoint) and has the routing follow them. The result
  is one road and one more variant, which competes against the fastest one on
  the finished charging plan. Cost: at most two routing requests, and only if
  something fits. Can be switched off with the checkbox „Meine gefahrenen Strecken mitrechnen" ("Include my driven routes").
- **TomTom as an advisor.** OpenRouteService remains the source of every stored
  route (the consumption model needs elevation and speed per segment, TomTom
  provides neither). TomTom answers two questions this service does not
  know: *What other ways are there?* (up to five alternatives, without the
  100 km limit) and *What does the traffic cost?* (per route, from live traffic).
  Only suggestions that no other route beats in both distance and time
  are followed up — on four of five measured routes every
  alternative was beaten, which saves most of the requests. The
  traffic delay is queried for each finished route by using waypoints to force
  TomTom onto the same road, and it enters the ranking
  („insgesamt schnellste" — "fastest overall" — includes traffic); it is shown on every variant card
  and, even for a single route, as a tile next to the key figures. For a
  trip loaded from the list there is none — traffic from yesterday would be
  worse than none. **Nothing from TomTom is stored**:
  its terms only allow results to be cached briefly and forbid derived
  databases. A suggestion only turns into waypoints that the routing follows; the delay is in
  the response to the browser and nowhere else. If TomTom fails, planning
  continues without it. The key is part of the URL, which is why the
  exception itself is never logged.
- **Departure time.** Empty means now; with a time, traffic and weather apply
  to it. Traffic comes as a time-dependent forecast from TomTom (`departAt`):
  on Friday at four a different road is the fastest than on Sunday at three
  (measured on Reutlingen - Hamburg: 723 instead of 712 km), and traffic costs
  +28 instead of +5 minutes. The weather is the hourly forecast, **per
  waypoint for the hour of arrival there** (departure plus a share of the
  travel time); a trip tomorrow morning is not calculated with today's
  afternoon weather, and heating is the biggest single item in the cold. The
  time goes out as UTC with a Z, never as local time without a zone. Rejected are
  the past (more than ten minutes back) and anything beyond 60 days; the weather
  only reaches 15 days, after that the current weather applies, and that is noted in the log.
  **Traffic is the whole effect** — travel time with traffic minus travel time
  in free flow — not the field `trafficDelayInSeconds`: that only means the
  real-time delay and was at +6.5 min for Friday 4 pm (time-dependent
  it is 28), at 0.0 in 90 days. For "now" this makes it the more complete
  number (+21 instead of +12.9 min on Reutlingen - Hamburg).
- **Plan charging stops** — the time-optimal sequence of stops and charge amounts:
  Pareto Dijkstra over `(charge point, arrival SoC)`, after which the charging
  increments are shifted on a fine grid into the steep part of the charging curve.
  For every stop the fallback location is shown that is still reachable
  without charging. A leg only counts as drivable if the state of charge
  *en route* stays above the reserve — over a pass the balance at the end
  looks harmless otherwise.
  **A stop costs five minutes before the first electron flows** —
  parking, cable, authorisation. Without this item the objective function is
  blind to the number of stops, and because a battery charges much faster at 10 %
  than at 60 %, it then always becomes cheaper to spread the same energy over many
  short stops. The planner did exactly that: ten stops instead of four,
  six of them under four minutes — optimal on paper and in reality
  three quarters of an hour slower.
- **Live tracking** — measurement points in, actual against planned, running
  consumption factor, and the reserve marker moves along. A simulator replays
  the trip with adjustable extra consumption *and* adjustable travel time,
  so this can be tested without a car.
- **Calculate with the measured speed instead of the guessed one.** The slider
  before departure is an estimate; the phone knows better. As long as no
  state of charge has been reported yet, the remaining distance is therefore not scaled
  but **recalculated** — with the speed actually driven and the
  weather of now instead of at departure. A factor would not do here:
  air drag goes with v², rolling resistance almost linearly, the
  auxiliary loads not with speed at all but with time — and that
  *drops* when you drive faster. As soon as a measured consumption is available,
  that one applies, because it already contains the effect of speed.
- **Replanning while driving** — the actual goal of the project. If one
  of the triggers fires, the remaining route is replanned from the current position:
  with the measured consumption, the measured travel time and the
  state of charge that is really there. If something changes, jolt says so in
  one sentence — and otherwise stays silent.

| Trigger | Threshold |
|---|---|
| Next charge point reported as occupied | immediately |
| Reserve will be reached before the destination | immediately |
| More than 500 m off the route | after 1 minute |
| Arrival state of charge at the next stop deviates | > 5 percentage points |
| Arrival time shifts (traffic jam) | > 10 minutes |

- **Notification to the phone** when the plan changes — even with the screen
  dark, via Web Push. And only then: a message that comes with every
  measurement gets switched off after ten minutes.
- **Learning from driven trips** — at the end of each trip the vehicle's
  correction factor is updated, damped so that a single
  trip with a roof box does not bend it permanently. Charging sections are excluded:
  anyone who recharges forty percentage points on the way sees a loss at the end
  that is too small by those forty.
- **Record trips instead of planning them** — the path for the case where
  planning is not worth it: a known short route, driven a few times, is
  the cleanest measurement there is. Route, elevation profile and forecast are created
  afterwards from the measurement points. If you forget to end it, the server does it
  itself — with a recording it would otherwise be a total loss, because until then
  the trip is a shell with empty geometry. A charging break extends the
  deadline, otherwise the cleanup cuts apart a trip that is about to continue.
- **Read the dongle directly** — via Web Bluetooth, without an intermediate app. On iOS
  this requires the browser **Bluefy**; Safari does not support Web Bluetooth —
  or the native app via TestFlight, which is built without a Mac by GitHub Actions
  ([`ios-einrichten.md`](ios-einrichten.md)).
  All readings are shown live in the dashboard, with the age of each value: a
  frozen display otherwise looks like a running one. What the car delivers
  and how is described further below.
- **CarPlay display in two styles** — the tiles of the CarPlay dashboard
  are images, and `frontend/tiles.js` draws them on a canvas: **A –
  Instrument** (pointer arc with scale for state of charge and regeneration, bars
  over a centre line, arrival with arrow, range bar) and **B –
  Telemetry** (numbers on the left, LED segment bars, arrival as deviation from
  zero, stops as a route band, histories as area). Colours follow the
  state (state of charge below 35 % yellow, below 20 % red, …). The style is chosen in the
  settings; there a preview shows both styles with sample values. The
  images travel as PNG in the display model (`tileImages`) to the CarPlay scene;
  without an image Swift draws as before. Because they are created in the UI,
  a change to the look is a change on the server, not a new app build.
- **Settings and dongle diagnostics** — the „Einstellungen" ("Settings") tab collects
  what is rarely needed: account and server, notifications, the
  dongle options (read only while driving, disconnect, forget device) and the
  storage (pending measurement points, reset app cache). The **dongle diagnostics**
  show live: connection (access, device, drops), per measured quantity the last value,
  age, hits (`ok`), empty responses and failures including response time, the
  round statistics, the dongle's log (filterable to anything unusual), a
  command console and a **report to copy** — the way to show somebody else
  why a value is missing. The dongle module keeps its own books for this
  (`joltObd.diagnose()`), regardless of which view connected it.
- **Distance from the odometer** instead of from GPS. At a
  twelve-second interval there are a hundred and sixty metres between two points at
  country-road speed, and the straight line cuts off every curve; a
  dead zone tears out a whole stretch at once. The counter in the car knows
  neither. Because consumption is calculated in kWh **per hundred kilometres**,
  the error would otherwise go straight into the correction factor.
- **Consumption from the vehicle's energy counters.** They count over the
  lifetime what went into and out of the battery; their
  difference over a stretch of the trip is the energy consumed — with 0.117 Wh
  resolution instead of the 339 Wh of one state-of-charge step. Almost three thousand times
  finer, which is why the bar plot shows consumption per **minute** instead of
  per five.
- **The measured battery capacity beats the brochure figure.** The profile holds
  what the manufacturer states for a new vehicle; the car reports what
  this battery can do today — on the ID.Buzz 73.8 instead of 77 kWh after 60,000 km. Every
  conversion between state of charge and kilowatt-hours hangs on this number.
- **Weigh charging time against cost.** A time value in euros per hour makes
  both comparable: anyone who charges ten minutes longer but saves a stop
  and stands at the cheaper provider may come out ahead. In addition a
  bonus for large charging parks (the risk of standing in front of an occupied charger
  falls with the number of points) and for preferred providers — both as a weight, never
  as an exclusion.
- **Connection for an in-car logger** — a device permanently installed in the
  vehicle cannot know the session ID of a trip; it only comes into being when
  you set off in the app and changes with every trip. It therefore identifies itself with
  a long-lived **logger token of the vehicle** (`POST
  /api/live/report`, "melden" = report), and jolt finds the running trip itself. If the car is
  parked, that is not an error but a response with `aufgenommen: false` ("recorded: false") — an
  unattended device that meets error responses logs errors
  or switches itself off.

- **Trailer and top speed.** A trailer belongs to the trip and
  brings mass *and* its own drag area (c_w times A in m²) —
  no bending of the car's cw. In addition a hard speed limit, on the
  trip (combination: 100 km/h) and on the vehicle; the smaller one applies. The
  speed slider runs into it instead of calculating with 165 km/h, and the
  travel time is stretched by what the limit costs. Recordings are
  exempt (driven is driven), and jolt does not learn a vehicle factor
  from a trip with a trailer. The defaults for the area are
  estimates until a recorded trip confirms them.
- **The dongle only asks while driving.** Whether the car is locked cannot
  be found out without asking it — and asking a locked car sets
  off the alarm. jolt therefore infers from the phone's movement:
  from 15 km/h (twice in a row) you are in the car, then it reads and
  reconnects the dongle if needed; after ten seconds of standstill
  nothing is asked any more (the connection stays, traffic lights and jams cost no
  reconnect); anyone who walks away more than 25 m or stands for three minutes has their dongle
  disconnected. A failed attempt with no dongle in range (bicycle, bus) stops
  after eight attempts until the next halt. The counters in the car run over
  the lifetime, so a gap at standstill costs no consumption. This can be switched
  off with the checkbox „Dongle nur beim Fahren lesen" ("Read dongle only while driving").
  **The earlier signal is the 12 V voltage:** `ATRV` is measured by the ELM chip itself,
  no frame goes onto the CAN bus, so jolt may ask it even on a locked
  car. As long as the car is on or charging, the DC/DC converter keeps the
  voltage up; when it goes off, it drops within seconds — before locking.
  Two low values in a row at standstill, compared with the mean of the
  last trip (no fixed threshold), disconnect the dongle immediately. The voltage
  is stored as `batt_v` on every measurement point, so that the threshold can be checked against real
  trips — also on points without a vehicle query, because it is precisely at
  standstill that it drops on switch-off. A recording keeps the dongle even if
  the car is still asleep at the start and does not answer the first query.
  If the car charges locked, the voltage stays up;
  then standstill and distance apply. What cannot be detected is someone who locks in the first moment after
  stopping without the car ever having been "off".
- **Location with a locked iPhone** — in the iOS app via the plugin
  `@capacitor-community/background-geolocation` (`CLLocationManager` with
  background mode) instead of `watchPosition`, which the WebView freezes on
  locking. The measurement points go through the same queue. An
  older app without the plugin falls back to the browser location. So far only
  tested against mocks (`tools/check_location.js`); whether iOS really keeps the app
  alive is shown by a drive.
- **Buffer measurement points** — every point carries its measurement time and first goes into
  a queue (`localStorage`), from there in batches of 100 to
  `POST /api/live/{id}/points` ("punkte" = points). Without a network the points stay put and later go out
  in measurement order; planning happens only at the last point of a
  batch, because a plan from a position ten minutes old would be
  outdated by the time it appears.

**Not there yet**: occupancy data for the charge points (it exists by now, see
„Next steps") and an evaluation of whether the background location on the iPhone really
keeps running.

---

## Getting started

### Locally, with nothing

```bash
pip install -r backend/requirements.txt
cd backend && python -m uvicorn app.main:app --reload --port 8322
```

Runs against SQLite and without an API key. Without `ORS_API_KEY` jolt calculates with
**made-up demo routes** — the whole chain can be played through,
but the route is the straight line. The UI says so at every point.

### With real routes

Get a free key (2,500 requests/day):
<https://openrouteservice.org/dev/#/signup>

```bash
export ORS_API_KEY=…
```

### In Docker

```bash
cp .env.example .env      # enter DB_PASSWORD, APP_PASSWORT and ORS_API_KEY
docker compose up --build
```

Then at <http://localhost:8322>. The database lives on the host under
`/opt/docker/jolt/db` — adjust in `docker-compose.yml` if needed.

### Deployment (Unraid, no ssh)

Every merge to `main` builds the image in GitHub Actions and pushes it to
`ghcr.io/cali1205/jolt:latest` (only when all checks are green). A Watchtower
container from the same `docker-compose.yml` looks for a new image every night
at 04:00 (`WATCHTOWER_SCHEDULE`, `TZ`) and restarts `jolt-app` only. Database
migrations run at app start.

One-time setup on the host: the package must be public (GitHub → Packages →
jolt → Package settings → Change visibility), then in the project folder
`docker rm -f jolt-app && docker compose up -d`. After that nothing needs to
be done by hand. Update right away: `docker exec jolt-watchtower /watchtower --run-once --label-enable`
or restart the container from the Unraid UI.

A database backup is **not** part of this; take one before risky migrations.

### Import chargers

Download the file „Ladesäulenregister" (CSV) from the [charging station map of the
Bundesnetzagentur](https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html),
then:

```bash
./tools/import_bnetza.py ladesaeulenregister.csv
OCM_API_KEY=… ./tools/import_ocm.py AT,CH 5000 50   # optional, for other countries
```

Without this step the list „Ladepunkte entlang der Route" ("Charge points along the route") stays empty — the
table is simply not yet filled on a fresh installation.

`import_ocm.py` queries Open Charge Map country by country. For a large country
(e.g. France) OCM's `offset` pagination does not reliably continue when there are very many
hits — some of the charge points then remain unreachable,
no matter how high the limit is set. For one specific route there is the
alternative `import_ocm_route.py`, which instead queries several smaller radii
along the actual route geometry:

```bash
OCM_API_KEY=… ./tools/import_ocm_route.py <trip_id> 30 50   # radius 30 km, from 50 kW
```

The `trip_id` is in the response of `GET /api/trips` ("fahrten" = trips) once the
route has been calculated in the app.

### Notifications to the phone

```bash
./tools/push_keyname.py      # generates a VAPID key pair
```

Copy the three output lines into `.env` and restart jolt.
After that the app asks for permission once when a live trip starts.
Whether it works is told by `POST /api/push/probe` — the message must arrive on the
device, even with the screen dark.

Two things it otherwise fails on: the browser only allows notifications
over **HTTPS** (`localhost` excepted), and on iOS the app must have been
added to the home screen. The **private** key stays on the
server; whoever has it can send to the registered devices in the name of this
installation.

---

## Configuration

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | If missing, SQLite is used (`jolt_dev.db`). |
| `APP_PASSWORT` | Access to the app (password). **Empty means: no login.** Appears as a warning in the log at startup. |
| `ORS_API_KEY` | openrouteservice. If missing, demo routing applies. |
| `OCM_API_KEY` | Only for the Open Charge Map import. |
| `TOMTOM_API_KEY` | Optional. TomTom as advisor for alternatives and traffic. Without it planning works as before. |
| `VAPID_PRIVATE_KEY` | Web Push. If missing, notifications are off. |
| `VAPID_PUBLIC_KEY` | The same key, public half — the browser needs it. |
| `VAPID_SUBJECT` | `mailto:` or `https:` — whom the push service can reach. |
| `TRUSTED_PROXIES` | IPs of the reverse proxy that may set `X-Forwarded-For`. |
| `RATE_LIMIT_PER_MIN` | Requests per minute and IP (default 120). |
| `ENABLE_API_DOCS` | `1` enables `/api/docs`. Default: off. |

---

## Checks

All scripts run without a network, without Postgres and without API keys:

```bash
./tools/check_model.py     # physics: air density, v², gradient, pass, cold, charging curve
./tools/check_optimizer.py # charging plan: reserve, gaps, charger choice, fallback
./tools/check_sources.py    # translate foreign report formats - and reject junk
./tools/check_replanning.py  # live: triggers one by one, replanning across the whole chain
./tools/check_push.py       # Web Push: keys, encryption, subscriptions, cleanup
node tools/check_buffer.js  # measurement-point queue in the frontend: dead zone, resubmission
./tools/check_backend.py    # whole chain: schema, import, route, corridor, charging plan, live
```

`check_model.py` does not check against fixed numbers but against the ratios
that must hold — for instance that 130 km/h is more than 10 % above 110 km/h, but
below the pure v² factor, or that a pass costs more than flat ground, even though
you arrive back at the starting altitude.

`check_optimizer.py` does not trust the planner: a second, independent
recalculator drives every finished plan kilometre by kilometre and checks
whether the state of charge falls below the reserve anywhere. It also checks against
the two cases where a greedy planner fails — a long gap without
a fast charger and the choice between a near weak and a farther
strong charger.

`check_sources.py` mainly checks what goes wrong. A report that
is correct is the boring case; the interesting ones are the missing field, the
timestamp in milliseconds instead of seconds and the one from 1970 when
a microcomputer without a network starts. Foreign data is broken until proven
otherwise, and a translator that does not catch this only moves
the error — it then ends up as a 500 in the log or, worse, as silent
nonsense in the energy profile.

`check_replanning.py` checks every trigger individually — above its threshold
it must fire, below it stay silent; a trigger that always fires is as useless
as one that never does. Then the whole chain: calculate a trip, create charge points,
replay with extra consumption and with traffic jams, and see whether the plan
changes, stays valid and does *not* change at every measurement.

Last, a trip in which charging really **happens**. The simulator never does that
— its state of charge falls monotonically to zero — and so the
normal case of every long trip went unchecked: stop, charge, drive on. The
energy profile contains driving time exclusively; the charging time is in the plan. Anyone who
holds the wall clock against it unfiltered reports, after the first charging stop,
a delay equal to the charging duration — permanently, because it is never made up.
The trigger „Ankunft verschiebt sich" ("Arrival shifts") would then stand above its threshold for the rest of
the trip. A message that always comes gets switched off.

`check_push.py` checks everything before the network hop — and the round trip through
the encryption is the core: the payload is encrypted for a rebuilt
browser subscription and decrypted again with its private key. If the plaintext
comes back, the path according to RFC 8291 is right. What the script
does **not** check is the hop to the push service itself; for that there
is `POST /api/push/probe` with a real device.

`check_backend.py` drives a simulated route with 25 % extra consumption and
checks that the reserve marker moves forward. That is the touchstone of the
live feature. In addition it records what the check scripts themselves could not
see: that every reference in the HTML carries a version (the cache bug was
there four times), that every read measured value has a label, and that
every tool in `tools/` finds the package in **both** layouts — in the repo
under `backend/app`, in the image next to it as `app`.

```bash
ORS_API_KEY=… ./tools/probelauf.py   # not a check script, a trial run ("Probelauf")
```

`probelauf.py` is a different tool from the six above, and the
difference is the purpose. A check script secures what you already know; this
run is meant to find what nobody has thought of yet. It asserts nothing, it
drives a real route with real charge points, then records a trip
the way the dongle sends it — with a charging break, dead zone and values that
fail individually — and at the end shows what is not right.

It paid off: four bugs came out of it that none of the six
check runs had seen, because they all work with short trips **without a charging stop**.
The most expensive was a learned factor that came out too low on every trip
with a charging stop — and because it stayed within the plausibility limits,
nobody noticed.

Even more can be checked without a car: under `/obd`, „Alle Werte prüfen" ("Check all values") reads
the complete set once and holds each value against what would be physically
plausible. The sharpest part is the **cross-check** — discharge counter
divided by odometer gives the lifetime consumption, and if that lands within
12 to 40 kWh/100 km, both formulas are right. Two independently read values
check each other, at standstill. That is exactly how a sign error came to light
that the range check had let through.

---

## Structure

```
konzept-routenplaner.md   The concept with the reasoning behind every decision
backend/app/
  geo.py      Haversine and bearing - knows nothing, needed by everyone
  models.py   SQLAlchemy · database.py · deps.py · security.py
  energy/     model.py (physics) · profile.py · weather.py
              calibration.py · charge_phases.py (driving and charging sections)
  routing/    provider.py (interface) · ors.py · demo.py · corridor.py
  charging/   optimizer.py · curves.py · prices.py · availability.py
              chargers_import.py
  live/       session.py · replanning.py · recording.py · cleanup.py
              channel.py (WebSocket) · simulator.py
              sources/  translate foreign report formats (jolt.py · abrp.py)
  push.py     Web Push: keys, subscriptions, sending
  routers/    auth · vehicles · route (incl. /charge-plan) · chargers · live · push
frontend/     index.html · core.js · app.js · map.js (own pan-and-zoom map)
              route.js · live.js · trips.js · vehicle.js
              obd.html · obd-core.js · obd.js  (dongle, separate page)
              sw.js (offline shell and push receiving)
tools/        import_bnetza.py · import_ocm.py · import_ocm_route.py
              push_keyname.py · examine.py (framework of the check scripts)
              check_model.py · check_optimizer.py · check_sources.py
              check_replanning.py · check_push.py · check_backend.py
              probelauf.py (not a check script - see „Checks")
```

**The layers only reach downward.** `geo` at the very bottom (knows nothing),
above it `energy`, `routing`, `charging`, `live`, and on top the routers. The
import graph is free of cycles; `geo.py` deliberately sits next to `models` and not
in one of the layers, because otherwise `routing` would have to reach into the physics
for a distance, or vice versa.

**The dongle has its own page.** `/obd` only works in a browser
with Web Bluetooth, and a control that stays mute in Safari does not belong in
the main UI. `obd-core.js` is the building block — the list of
measured values, the handshake, the assembly of multi-part responses;
`obd.js` is the diagnostics page around it, and `live.js` uses the same
building block while driving.

**The optimizer knows neither database nor network.** It receives a fully
calculated route profile and a list of charging options — it needs nothing
more. That is the reason `check_optimizer.py` manages without either and
a hypothetical location ("what if a 300 kW charger stood here?")
is one line of code instead of a database entry.

This is made possible by a property of the consumption model: the
energy demand of a leg does **not** depend on the state of charge — an EV does not
get heavier when charging. A single pass of the model is therefore enough for
all variants; the optimizer reads the demand of each leg as the difference
of two cumulative values. That is why a second charging plan with a different
radius costs neither a routing nor a weather request.

**No PostGIS.** The only geo query is "all charge points in the corridor around a
polyline". An index on `(lat, lon)` with bounding-box prefilter and
haversine solves this in milliseconds with around 150,000 German charge points —
and preserves the SQLite fallback for local development.

**No map library.** `frontend/map.js` is two hundred lines for
tiles, a line, markers and zooming by dragging. Including a library
would mean putting it into the repo (the content security policy forbids CDNs)
and maintaining it permanently — for a fraction of its functionality.

Map tiles come from OpenStreetMap; the attribution is shown below the map
because it is required.

---

## What the car provides — the MEB data identifiers

An ELM327 dongle on a MEB vehicle (ID.3, ID.4, ID.Buzz, Enyaq,
Q4 e-tron, Cupra Born) reads **nothing** via the standardised OBD2 PIDs — they are
meant for combustion engines. Everything goes through manufacturer-specific
UDS queries, and the knowledge about them is in three sources that
partly contradict one another:

* [spot2000/Volkswagen-MEB-EV-CAN-parameters](https://github.com/spot2000/Volkswagen-MEB-EV-CAN-parameters)
  — 193 parameters with addresses; for many the conversion is missing
* [meatpiHQ/wican-fw](https://github.com/meatpiHQ/wican-fw/blob/main/vehicle_profiles/vw/ev_meb.json)
  — vehicle profile with formulas, names the ID.Buzz explicitly
* [codingABI/id3esp32obd2](https://github.com/codingABI/id3esp32obd2)
  — ESP32 logger, reads the CAN frames directly

Where they contradict one another, the section below states which version jolt follows and why.
The list itself is in `frontend/obd-core.js`; **that is the
reference**, this table is its explanation.

### Target addresses

A vehicle speaks on **two frame widths**. That is the reason why
climate and battery values did not arrive at all at first: the handshake sets `ATSP7`
— 29 bit —, and nobody listens on an 11-bit identifier then.

| Device | Protocol | ATCP | ATSH | ATCRA | ATFCSH |
|---|---|---|---|---|---|
| Battery (BMS) | 7 (29 bit) | `17` | `FC007B` | `17FE007B` | `17FC007B` |
| Vehicle | 7 (29 bit) | `17` | `FC0076` | `17FE0076` | `17FC0076` |
| DC/DC converter | 7 (29 bit) | `17` | `FC00B9` | `17FE00B9` | `17FC00B9` |
| Climate | **6 (11 bit)** | `00` | `746` | `7B0` | `746` |
| Battery (capacity) | **6 (11 bit)** | `00` | `710` | `77A` | `710` |

`0x746` and `0x710` fit in eleven bits, `0x17FC007B` only in 29. For the two
lower rows jolt briefly switches to `ATSP6` and back in the `finally`.

### Flow control — the trick without which half is missing

```
ATFCSH<header>   ATFCSD300000   ATFCSM1
```

If a response does not fit into one CAN frame, the asker must send back a
flow-control packet. The ELM327 does this itself — but only if
it knows the header, and with the MEB addresses it guesses wrong. **Without these three
commands every multi-part response fails silently.** Affected were
battery current, energy counters, range and compressor — four of the
most interesting values.

### The measured values

`b[0]` is the first byte **after** the acknowledgement (`62` + data identifier), i.e.
`g_dataBuffer[0]` at codingABI and `B4` at spot2000/WiCAN.

| Value | DID | Device | Rate | Conversion | Remark |
|---|---|---|---|---|---|
| State of charge (raw) | `22028C` | BMS | every | `b0/2.5` | Required — without it the round is discarded |
| Voltage | `221E3B` | BMS | every | `[b0:b1]/4` | ~377 V at 79 % |
| **Current** | `221E3D` | BMS | every | `([b0:b3]−150000)/100` | **multi-part**; negative = discharge |
| **Total discharged** | `221E32` | BMS | every | `\|[b12:b15]\|/8583.07` | **multi-part, signed** |
| Total charged | ↑ | BMS | every | `[b8:b11]/8583.07` | from the same response |
| Charge limit | `221E1B` | BMS | every | `[b0:b1]/5` | |
| Operating mode | `227448` | BMS | every | `b0` | Bit 2 = charging |
| Heating current (PTC) | `221620` | BMS | every | `b0/4` | battery heater |
| Speed | `22F40D` | BMS | every | `b0` | |
| Battery temperature | `222A0B` | BMS | 10 | `b0/2−40` | more accurate than the outside temperature for the charging curve |
| Auxiliary loads | `220364` | Vehicle | every | `[b0:b1]/10` | everything except the drive |
| **Odometer** | `22295A` | Vehicle | every | `[b0:b2]` | whole km; corrects the GPS distance |
| DC/DC current | `22465B` | DC/DC | 10 | `[b0:b1]/16` | |
| Battery capacity | `222AB2` | Battery | 40 | `[b0:b3]/1310.77/1000` | measured, not brochure |
| Range | `222AB6` | Battery | 10 | `[b0:b1]` | **multi-part** |
| Outside temperature | `222609` | Climate | 20 | `b0/2−50` | |
| Inside temperature | `222613` | Climate | 20 | `[b0:b1]/5−40` | |
| **Climate compressor** | `220800` | Climate | 20 | `[b5:b6]` W | **multi-part**; `b0` bit 0 = on, `[b3:b4]` = speed |

"Rate" is the round count: `every` means every measurement, `20` every twentieth.
Values that change slowly or cost a protocol switch are read rarely.

### Where the sources contradict each other

| Value | jolt follows | discarded |
|---|---|---|
| **Current** `221E3D` | spot2000 + codingABI: `([b0:b3]−150000)/100` | WiCAN: `(150000−[b1:b5])/100` — yields −383,731 A on the vehicle |
| **Range** `222AB6` | codingABI: `[b0:b1]` → 297 km | WiCAN: `[b1:b2]` → 10,497 km |
| **Capacity** `222AB2` | codingABI: four bytes | WiCAN: two bytes × 50 — the same formula, coarser |

### What no source knew

The **compressor power** is in none of the three lists; spot2000 lists
`220800` with "equation missing". A differential measurement on the vehicle
settled it — once with and once without the compressor running:

```
          b0    b1b2   b3b4   b5b6   b7
off     0x10       0      0      0    0
on      0x51    9408   9408   2618   14
partial         3648   3712    935    5
```

`b5b6` is the power in watts (0 / 935 / 2618), `b1b2` and `b3b4` run
alike and much higher — setpoint and actual speed. As watts, 9.4 kW would be too much for
a climate compressor. The toolbox for this is under
`/obd` → „Klimakompressor eingrenzen" ("Narrow down climate compressor").

### Not implemented

* `222AB8` **energy content** — multi-part, no source gives a conversion.
  Consumption does not need it: the difference of the discharge counter is
  more accurate.
* Cell voltages and temperatures (spot2000 lists over a hundred of them) — irrelevant for
  route planning.

### Two states of charge

MEB vehicles deliver **two**: that of the battery management and that of the
display. `reserve_soc` and `target_soc` refer to the display one.

```
gross = b0 / 2.5
display = gross · 51/46 − 6.4        (clamped to 0…100)
```

Anyone who takes the wrong one is permanently off by the hidden buffer —
at 79 % display that is a good two percentage points.

---

## Next steps

**Occupancy of the charge points.** The comment in `charging/availability.py` says
real occupancy data is not available to a private project. That is no
longer true — at least not for Germany. The
[OCPDB of MobiData BW](https://mobidata-bw.de/dataset/e-ladesaulen) delivers
OCPI 3.0 **without a key and without registration**, nationwide, with
live status for over ten thousand sites. Measured afterwards, around
13 % of the charge points change per hour — it is real data, not a snapshot.

Two catches: there is no spatial filtering (`bbox` is silently
ignored), so the inventory has to be synchronised periodically instead of queried per
request. And for **France** there is nothing — the national
access point lists zero IRVE datasets under "temps réel".

The prepared `AvailabilitySource` interface fits: an adapter that
returns `State(free=…, source="ocpi")` instead of `Unknown`. The effort lies
less in the OCPI part than in matching against jolt's own charge-point inventory — the
live entries carry an `evse_id`, jolt's OCM import does not store it.

**Important here:** the redundancy bonus (`redundancy_bonus`) stays. Live data exists for about a
tenth of the sites and for France not at all; an optimizer that
penalises sites without live data systematically chooses the wrong ones on a trip through France.

**What else the compressor offers.** `220800` delivers, besides the power,
setpoint and actual speed; one value is still uninterpreted (`b7`, the same quantity
as the power in the ratio 1:187). And `222AB8` (energy content) keeps
waiting for a conversion — it is not needed, the difference of the
discharge counter is more accurate.
