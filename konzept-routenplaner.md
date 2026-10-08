# jolt — Concept

A route planner for electric cars whose real job is not planning but
**keeping the plan up to date**.

---

## 1. The problem

Every charging planner computes a plan when you set off: charge here, for this
long, then drive on. At that moment the plan is correct. After eighty
kilometers it no longer is.

Small things are enough, and they all add up in the same direction:

- You drive 135 instead of the assumed 120 km/h.
- It is 2 °C instead of the 15 °C at which the manufacturer's figure was
  measured.
- A 25 km/h headwind.
- The heating is on, the battery is cold and does not take the promised 150 kW
  at the fast charger.
- The rest stop in the plan has four charging points and all of them are
  occupied.

Taken alone, each of these costs a few percent. Together they shift the arrival
SoC at the next stop by ten to twenty percentage points — and that is exactly
the order of magnitude in which "arriving relaxed" turns into crawling along
the hard shoulder at 90 km/h.

The usual way to deal with this is a generous buffer: you plan with 20 %
remaining charge instead of 8 %. That works, but it costs time on every trip —
the buffer is paid for at the charger; not in the steepest part of the charging
curve, but the additional stops add up.

**jolt's thesis:** If you keep adjusting the plan to reality while driving, you
do not need the buffer. The solution is not a better starting plan but a plan
that notices when it has become wrong.

---

## 2. What jolt does differently

### 2.1 Consumption is calculated, not estimated

Almost all planners work with a flat consumption in kWh/100 km, often with a
slider for "driving style". That is why they are off in winter and in the
mountains: a flat value cannot know that the next 40 km climb 900 meters.

jolt splits the route into segments and calculates the physics for each one:

```
F_roll  = c_rr · m · g · cos(θ)
F_air   = ½ · ρ(T, h) · c_w · A · (v + v_head)²
F_slope = m · g · sin(θ)

E_segment = (F_roll + F_air + F_slope) · s / η_drive         if > 0
E_segment = (F_roll + F_air + F_slope) · s · η_regen         if < 0
E_aux     = P_hvac(T_outside) · t_segment
```

Three important points that a flat value cannot capture:

**The `v²` in the air drag.** The difference between 110 and 130 km/h is not
18 % more air drag but 40 %. Because air drag accounts for the largest share of
consumption on the motorway, the choice of speed is the strongest lever the
driver has — and the only one he can still pull *while* driving. A planner that
does not model this cannot answer the question "will I still make it if I drive
110?". But that is exactly the question you ask yourself at 12 % remaining
charge.

**The elevation.** `sin(θ)` is only 0.05 at a 5 % gradient — but for a 1.5 t
vehicle that is 735 N of additional force, more than rolling and air resistance
combined. Uphill an EV uses dramatically more; downhill it gets part of it back
through regeneration, but only part (`η_regen` ≈ 0.7). Over a mountain pass the
balance is clearly negative, even though you end up back at the starting
altitude. That is why the model needs a real elevation profile and not just a
distance.

**The auxiliary loads.** `P_hvac` is almost zero at 20 °C and between 2 and
4 kW at −5 °C. What matters is that this power depends on **time**, not on
distance: in a traffic jam the heating costs just as much as at 130 km/h, only
without any kilometers covered. That is why winter trips with traffic jams hit
the forecast hardest.

### 2.2 The model learns your own car

The physics above needs vehicle parameters — `c_w`, frontal area, rolling
resistance, efficiency. Nobody knows these exactly, and they change with tires,
roof box, load and battery age.

That is why every vehicle has a **correction factor** that is derived from real
trips: predicted kWh against kWh actually used. After a few trips jolt knows the
specific car better than any database — including the roof box that has been on
top since Easter.

This is exactly where the OBD2 loggers come in: they supply the actual values
from which the factor is derived.

### 2.3 The plan is adjusted while driving

This is the live function, and it is the reason for this project.

While driving, measurement points keep coming in: position, SoC, speed, outside
temperature. jolt continuously compares two numbers:

- **Target SoC** — what the plan predicted at this point.
- **Actual SoC** — what the car reports.

From the deviation over the last few kilometers a running consumption factor is
derived, with which the rest of the route is recalculated. Replanning does not
happen on every measurement, but when one of these triggers fires:

| Trigger | Threshold |
|---|---|
| Predicted arrival SoC at the next stop deviates | > 5 percentage points |
| Forecast falls below the reserve | immediately |
| Next charging point reports as occupied or faulty | immediately |
| Vehicle leaves the planned route | > 500 m for > 1 min |
| Arrival time shifts (traffic jam) | > 10 min |

The reason for thresholds instead of "every time": a plan that changes every 30
seconds is not a plan. Someone who has just decided to take a break in 40 km
should not have to throw that over three times. A change has to mean something.

### 2.4 The charging curve is not a single number

"150 kW charging power" is a peak figure that applies between 20 and 40 % SoC.
At 70 % it may be only 60 kW, at 85 % only 35. Anyone who ignores the
difference plans too few, too long stops.

jolt stores a curve per vehicle as support points `(SoC %, kW)` and
interpolates between them. The actual power is then

```
P = min( curve(SoC), P_max_chargepoint, P_max_vehicle ) · f_temperature
```

This gives a rule that saves time: **better twice briefly from 10 to 55 % than
once for a long time from 10 to 90 %.** The last 30 percentage points often
cost more time than the first sixty. The optimizer (section 4) exploits this;
as a user you only notice that the stops are shorter than expected.

---

## 3. Where the data comes from

| Purpose | Source | Note |
|---|---|---|
| Route + elevation profile | openrouteservice | 2,500 requests/day free, `elevation=true` returns the elevation per waypoint |
| Chargers in Germany | Bundesnetzagentur charging station register (CSV) | official, complete, no key needed |
| Chargers worldwide | Open Charge Map | free API key, 300,000+ charging points |
| Temperature and wind | Open-Meteo | no key needed |
| Live SoC | manual / simulator at first | prepared for OBD2 loggers and manufacturer APIs |

Routing sits behind a narrow interface (`RoutingProvider`). A self-hosted
**Valhalla** is therefore later just a second adapter, not a rebuild — relevant
as soon as the daily quota of 2,500 requests gets tight or live replanning
calculates more often.

### Availability is the unsolved problem

Real occupancy data for public chargers is not freely available in Germany.
Whoever has it has it through **OCPI** contracts with operators or through
commercial aggregators. For a private project that is closed off for now.

jolt therefore handles this honestly instead of faking availability:

1. The availability source (`AvailabilitySource`) is an interface. An OCPI
   connection is later an adapter, not a rebuild.
2. As long as no data is available, **redundancy** counts: a site with eight
   charging points is preferred over one with two, even if it costs two minutes
   of detour. That is the best available approximation of "there is probably
   something free".
3. The user can report in the app that a site is occupied. This applies to the
   current trip and triggers replanning immediately.
4. For every stop a **fallback site** is planned as well, which stays reachable
   without recharging. If everything on site is occupied, nobody has to search
   anew.

---

## 4. Charging stop planning

*(Implemented in `backend/app/charging/optimizer.py`, tested by
`tools/check_optimizer.py`.)*

The task: find the sequence of charging stops and charge amounts that minimizes
the **total travel time**, subject to the constraints that the SoC never falls
below the reserve and that the desired target SoC is reached at the
destination.

This is not a shortest path, but a shortest path with one continuous decision
variable per node (how much is charged). The way there:

**Step 1 — Candidates.** All charging points in the corridor around the route,
filtered by connector type and minimum power. For each candidate the detour in
minutes (leaving + approach + return). Candidates with more than ~10 min detour
are dropped; they almost never win back the time at the charger.

**Step 2 — Graph.** Nodes = start, candidates (ordered by progress along the
route), destination. An edge `i → j` exists if the leg is drivable at all with
a full usable battery. Edge cost = driving time + detour time; the energy
demand of the leg comes from the consumption model.

**Step 3 — Search.** Dijkstra over the state `(charging point, arrival SoC)`.
Because the SoC is continuous, a **Pareto front** of labels `(time, SoC)` is
kept per node: a label is discarded if another one is there both earlier *and*
with more charge. This keeps the set of states small without coarsely
discretizing the SoC.

The charging time at a node follows from the charging curve:

```
t_charge(SoC_in → SoC_out) = ∫ (E_battery / P(s)) ds
```

**Step 4 — Post-optimization.** The solution is shifted locally: charging
strokes move into the steep part of the curve (roughly 10–60 %), as far as the
reserve allows. Typically this makes stops shorter and sometimes adds one —
faster overall.

**Step 5 — Fallback sites.** For every stop the best alternative stop that is
still reachable without recharging is determined.

### Why not simply greedy?

A greedy planner ("drive until the reserve is reached, charge wherever you
happen to be") is simple and usable on flat terrain. It fails systematically in
two places: before long gaps without a fast charger, where you should have
charged more *beforehand*, and when choosing between a 50 kW and a 300 kW site
twenty kilometers later. Both are exactly the cases in which a planner is worth
having — hence the effort with the Pareto front.

---

## 5. Structure

Deliberately the same conventions as `nest`: FastAPI + SQLAlchemy + Alembic,
PostgreSQL in Docker with an SQLite fallback for local development, a
vanilla-JS PWA without a build step, English comments that record the *why*.

```
backend/app/
  routing/    provider.py (interface) · ors.py · corridor.py
  energy/     model.py · weather.py · calibration.py
  charging/   curves.py · chargers_import.py · availability.py
  live/       session.py · channel.py (WebSocket) · simulator.py
  routers/    auth · vehicles · route · chargers · live
frontend/     index.html · map.js · route.js · vehicle.js · live.js
tools/        import_bnetza.py · import_ocm.py · check_model.py · check_backend.py
```

**No PostGIS.** The only geo query jolt needs is "all charging points in the
corridor around a polyline". An index on `(lat, lon)` with a bounding-box
pre-filter followed by a haversine calculation solves that in milliseconds for
around 150,000 German charging points — and it keeps the SQLite fallback that
makes local development possible without a running Postgres. PostGIS remains
the option as soon as isochrones are added.

---

## 6. Stages

**Stage 1 — what is there now**

- Vehicle profiles with charging curve, including templates for common models
- Charger import from Bundesnetzagentur and Open Charge Map, idempotent
- Route with elevation profile, weather along the route
- The consumption model, complete — including a range marker on the map: the
  point at which the SoC reaches the reserve
- Charging points in the corridor with detour time
- Live scaffolding: measurement-point endpoint, WebSocket, trip simulator,
  actual against target in the PWA

**Stage 2 — the optimizer** *(done)*

Section 4 in code: candidate graph, Pareto Dijkstra, post-optimization,
fallback sites. As `POST /api/trips/{id}/charge-plan` and as a charging plan in
the PWA.

Two things turned out differently than planned:

- The leg check does not look at the balance at the end of the leg, but at the
  **largest cumulative demand within the leg**. Over a pass the balance at the
  end looks harmless, because regeneration on the descent gives some of it
  back — but at the top the battery would still be empty. Without this
  distinction the optimizer plans legs that are not feasible in the middle.
- The fallback site may **dip into the reserve**, up to half of it. The plan
  itself never touches it; it arrives everywhere with at least `reserve_soc`.
  A fallback site that has to leave the full reserve untouched would therefore
  almost never be reachable — and the reserve is there for exactly this case.
  If there is none, the plan says so instead of concealing the gap.

**Stage 3 — live replanning** *(done)*

The triggers from 2.3 in full, in `live/session.py`; the replanning itself in
`live/replanning.py`. What is replanned is the **remaining distance** from the
current position with the current charge level — for the optimizer from stage 2
this is the same task as before departure, just with better numbers.

Three things were added that were not in the concept:

- A second factor for **time**. The consumption factor does not see a traffic
  jam: someone who is standing even uses a little more per kilometer, but the
  arrival time shifts by a multiple of that. Without a number of its own, the
  trigger "arrival time shifts" would not be available — and every arrival time
  in the replanned charging plan would be the one from the old plan.
- A **lockout** against replanning too often. The thresholds from 2.3 say
  *when* something is no longer right — they do not say when it is right again.
  A deviation of eight percentage points is still there at the next
  measurement, and at the one after that. Without a lockout, every single
  measurement would therefore recalculate. Urgent reasons (charger occupied,
  reserve not sufficient) always go through, everything else only again after
  ten kilometers.
- The simulator got a **simulated clock**. It plays back hours in seconds;
  measured against the real clock, any time factor would be nonsense. With
  simulated timestamps a traffic jam can be played through instead — and thus
  exactly the trigger that consumption never fires can be tested.

In addition **Web Push** (`push.py`): a plan change reaches the phone even with
a dark screen, because the service worker receives it when the page has long
been closed. Without a VAPID key the feature is off — the same attitude as with
`ORS_API_KEY` and `APP_PASSWORT`: what is not set up is not faked.

One decision that matters here: **a dead subscription is deleted, a disturbed
one is not.** A browser that has revoked the permission answers with 404 or 410;
the subscription is then permanently worthless. A 500 from the push service, on
the other hand, says nothing about the subscription — anyone who throws it away
for that turns notifications off permanently at the first disturbance, and
nobody notices why they no longer arrive.

**Stage 4 — real vehicle data** *(done)*

Connecting the OBD2 loggers or a manufacturer API. The data model
(`LiveSession` / `LivePoint`) accepts them unchanged — at *this* point nothing
actually changes.

The way there did need an addition, though, one that was overlooked in the
design: measurement points came in exclusively via
`/api/live/{session_id}/point`. That suits the PWA, which started the trip
itself and therefore knows the ID — but not a device installed in the car that
simply starts sending when it is switched on. The session ID only comes into
being at departure and changes with every trip; a dongle cannot know it. That
is why the *vehicle* now carries a long-lived logger token, and
`POST /api/live/report` (report) finds its running session itself.

An ELM327 does not read the charge level of an MEB vehicle via the standardized
OBD2 PIDs — those are geared to combustion engines — but via
manufacturer-specific UDS queries. The charge level is at service `0x22`, DID
`028C`, raw value divided by 2.5.

The address, on the other hand, is **not** the obvious 11-bit header `7E5`, but
a 29-bit identifier: ask `0x17FC007B`, `0x17FE007B` answers, and on the ELM327
that reads `ATSP7` · `ATCP17` · `ATSHFC007B` · `ATCRA17FE007B`. Until that was
established, every query returned `NO DATA` — the guess with `7E5` cost several
attempts. Which other values there are and which pitfalls come with them is
described in the README under "Was das Auto hergibt" (what the car gives up).

For this there is `live/sources/`, analogous to `routing/provider.py`: one file
per format, normalizing to a `RawPoint`. jolt's own format and that of Iternio
(ABRP) are included, because the latter is a de facto standard among electric
car loggers. **The translation knows no network** — it receives a parsed object
and returns a `RawPoint`. Whether that came from a POST, from a query to a
third-party service or from a file is a question of transport. That is exactly
why a new format can be added from a recorded response without sitting in the
car, and `check_sources.py` runs without anything else.

**The transport exists by now, and it manages without a third-party cloud.**

The ABRP app as sensor driver plus its telemetry API had been considered — for
an iPhone that seemed the only way, because Car Scanner only exports recording
files there. It would still have been a detour via a third-party service, and
the alternative — an ESP32 dongle with its own connection — would have had no
GPS.

What was built is a third way: **Web Bluetooth**, directly from the PWA. The
browser itself talks to the BLE dongle, the phone supplies the position, and
nobody else sees the data. On iOS this needs the **Bluefy** browser — Safari
does not support Web Bluetooth — and `requestDevice` strictly requires a user
gesture. Both are restrictions, but not ones that make a third-party service
necessary.

What was not foreseen and nevertheless turned out to be decisive: the vehicle
speaks with **two frame widths** (climate and battery on 11 bit, everything
else on 29), and without the three flow-control commands every response that
does not fit into one CAN frame fails — silently. Four of the most interesting
values were therefore missing for months, without anything saying anywhere that
they were missing.

`live/sources/` is nevertheless still right: a logger that speaks a foreign
format continues to report via `POST /api/live/report` — the Web Bluetooth route
is one source more, not the only one.

**Stage 5 — calibration from real trips** *(done)*

The correction factor from 2.2, fed from completed trips. It is updated when a
live session ends, damped, and only if the trip was long enough and the measured
factor was plausible.

What only the field test showed: **charging sections have to come out.** The
first version took charge level at the start minus at the end — anyone who
recharges forty percentage points along the way sees a loss that is forty too
small. The learned factor turned out correspondingly too low, and because it
stayed within the plausibility limits, nobody noticed. From a trip with
29 kWh/100 km the vehicle learned 12.

**Stage 6 — measuring instead of estimating** *(done)*

Not planned, but forced by the first real recording: in too many places there
was an estimate where the vehicle itself knows the number.

- **Distance** from the odometer instead of from GPS. For a quantity with the
  distance in the denominator, that is the difference between usable and
  misleading.
- **Energy** from the lifetime counters instead of from the charge level. One
  charge-level step is 339 Wh, one counter step 0.117 — almost three thousand
  times finer. Only with that is a consumption bar per minute a measurement
  instead of noise.
- **Battery capacity** from the vehicle instead of from the brochure. For the
  ID.Buzz 73.8 instead of 77 kWh after 60,000 km; every conversion between
  percent and kilowatt-hours hangs on this number.

The principle behind it applies beyond the OBD2 part: where a measured and an
assumed number are available, the measured one wins — and where no measured one
is available, the interface should say so instead of making the assumption look
like a measurement.

---

## 7. What jolt deliberately does not become

- **Not a navigation device.** The turn-by-turn directions come from the phone
  or the car. jolt answers *where and for how long* to charge — the rest is
  solved.
- **No payment function.** Charging cards and roaming are a business of their
  own.
- **No outside users.** Self-hosted, for your own vehicles. That allows simple
  auth and in-memory state instead of user management.
