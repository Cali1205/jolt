# Development

## Running it locally

```bash
pip install -r backend/requirements.txt
cd backend && python -m uvicorn app.main:app --reload --port 8322
```

Without `DATABASE_URL` jolt uses SQLite (`jolt_dev.db`); without `ORS_API_KEY`
it calculates with made-up demo routes (straight lines), and the UI says so.
For the frontend there is no build step: edit the files in `frontend/` and
reload. Count up `CACHE` in `frontend/sw.js` with every change to the shell,
otherwise installed apps keep the old copy.

## Checks

All scripts run without a network, without Postgres and without API keys:

```bash
./tools/check_model.py     # physics: air density, v², gradient, pass, cold, charging curve
./tools/check_optimizer.py # charging plan: reserve, gaps, charger choice, fallback
./tools/check_sources.py    # translate foreign report formats - and reject junk
./tools/check_replanning.py  # live: triggers one by one, replanning across the whole chain
./tools/check_push.py       # Web Push: keys, encryption, subscriptions, cleanup
node tools/check_buffer.js  # measurement-point queue in the frontend: dead zone, resubmission
node tools/check_pending.js # recording started/ended without network, delivered later
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

`probelauf.py` is a different tool from the check scripts above, and the
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

The complete list that CI runs is in `.github/workflows/ci.yml`.

## Structure

```
docs/concept.md   The concept with the reasoning behind every decision
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
as the power in the ratio 1:187).
