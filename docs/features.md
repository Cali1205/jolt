# What jolt does, in detail

The [README](../README.md) has the short list. This page explains each feature
and the reasoning behind it. Names in quotes are the German labels in the app.

- **Calculate consumption physically** instead of a flat kWh/100 km — air
  drag with v², gradient from the elevation profile, regeneration with realistic
  efficiency, heating by time rather than by distance. The difference between
  110 and 130 km/h is 40 % more air drag, not 18; a planner has to be able to
  model that, otherwise it is cosmetics.
- **Recording without network** — starting, recording and ending a trip does
  not depend on reception. The start asks the server for six seconds; if it
  does not answer, the recording runs locally (`frontend/pending.js`) and is
  created on the server with its real start time as soon as the network is
  back, then the collected points follow (the same start sent twice finds the
  recording it already made). Ending without network is remembered and
  carried out later, in the background. Every API call has a time limit
  (20 s) instead of the browser's minute, the vehicle list is kept for the
  next start, and the service worker serves the app from its copy when the
  network does not answer within 2.5 s. Planning still needs the server.
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
  ([ios-app.md](ios-app.md)).
  All readings are shown live in the dashboard, with the age of each value: a
  frozen display otherwise looks like a running one. What the car delivers
  and how is described in [obd.md](obd.md).
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
the next steps in [development.md](development.md#next-steps)) and an evaluation of whether the background location on the iPhone really
keeps running.
