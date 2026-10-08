# jolt as a native iOS app

Converting the frontend from a PWA to a native SwiftUI client. This document
records **why** that is necessary, **what** comes out of it and in which
order — it is the template for the implementation, not its result.

The state before the conversion carries the tag `pwa-stand-2026-09-05`.

---

## Why native at all

There are three things a web interface fundamentally cannot do on iOS, and all
three are no side issue for jolt:

**Bluetooth.** Safari does not implement `navigator.bluetooth` on iOS — for
privacy reasons, system-wide, for years and with no prospect of change. That is
why the PWA currently needs [Bluefy](https://bluefy.app): a third-party app that
builds a bridge via CoreBluetooth and emulates the web API in the page content.
This works, but it means that jolt's central function — reading the charge
level from the car — depends on an app that does not belong to us and whose
continued existence nobody guarantees.

**Location in the background.** As soon as the Safari tab is not visible, iOS
pauses position tracking. That is exactly why `frontend/live.js` contains the
screen wake lock with the silent video — a workaround for a problem that does
not exist natively. Recording a trip while the phone is in your pocket only
works with `CLLocationManager` and `allowsBackgroundLocationUpdates`.

**CarPlay.** Does not exist for web content, in no form. Apple only allows its
own templates there, via a `CPTemplateApplicationSceneDelegate`.

## The way there leads through Capacitor

It used to say here that a WebView wrapper solves none of this: it uses the same
WebKit and has the same missing Bluetooth API. The first half-sentence is true,
the conclusion drawn from it is not. With Capacitor, access does not go through
the web API but through plugins that execute native code — it is a real Xcode
project in which arbitrary Swift may live.

| Limit | Way via Capacitor |
|---|---|
| Bluetooth | `@capacitor-community/bluetooth-le` via CoreBluetooth |
| Keep screen awake | `@capacitor-community/keep-awake`, sets `isIdleTimerDisabled` |
| Location in the background | plugin via `CLLocationManager` |
| CarPlay | own `CPTemplateApplicationSceneDelegate`, real Swift work |

**What tips the balance is `obd-core.js`.** Further below, this document calls
the file the most valuable piece of the frontend and step 3 the touchstone of
the whole conversion — rightly so: in a translation, signs, scaling and byte
order silently go wrong, and a wrong value looks plausible. Exactly this risk
disappears if the file keeps running instead of being translated. It is 1055
lines long (at the time of writing), and of those **fourteen** touch the Web
Bluetooth API, bundled in `link`, `connectionBuildUp`, `detach`, `command` and
`connectWithoutDialog`. The rest is arithmetic with no browser connection.

So only the transport is swapped: `frontend/obd-ble-native.js` emulates the
subset of Web Bluetooth that is used and answers it via the plugin. For this,
the core gets a single new function, `bt()`, which decides at runtime where the
Bluetooth comes from. The numbers therefore stay the same, because it is the
same calculation.

**What Capacitor cannot do.** As soon as iOS suspends the app, the JavaScript
stops. The BLE connection survives and a location plugin keeps collecting
natively, but the reading loop for the CAN values no longer runs. "Trip with
the phone in your pocket" therefore exists for GPS, not for the vehicle data.
With the phone in its mount and the screen kept awake the case is moot — and
that is everyday use.

**SwiftUI remains the option behind it, not in front of it.** If the app holds
up in daily use and CarPlay is to be added, the path there is open, and it is
then easier to walk than today: the translated byte formulas could be checked
against a running native app on real trips instead of against Bluefy. The
honest reservation is that interim solutions often become permanent.

## What explicitly stays

**The backend does not change.** The app talks to the same FastAPI that already
serves the web interface today. No new endpoint is needed for the first step.

**The web frontend stays.** It is the access from the computer, it works, and it
is the fallback while the app does not yet hold up. Only once it does is a
rollback worth discussing — not before.

**`obd-core.js` is translated, not replaced.** It contains seventeen readings
with their data identifiers, the 11-/29-bit address switching and the byte
formulas, and part of it was worked out on the vehicle and not copied from a
reference. This file is the most valuable piece of the frontend.

---

## Structure

```
ios/
  Jolt.xcodeproj
  Jolt/
    App/            entry point, scenes, settings
    Network/        HTTP connection to jolt's API
    OBD/            CoreBluetooth + protocol (translation of obd-core.js)
    Trip/           live recording, background location
    Views/          SwiftUI: planning, live, trips, vehicles
    CarPlay/        CPTemplateApplicationSceneDelegate + status template
  JoltTests/        protocol tests against known responses
```

English identifiers as in the backend. That is not a quirk but keeps the terms
the same between the layers: what is called `consumption_factor` in the backend
should not be called something else in the app.

### Why a directory in the existing repository

And not a second one next to it: the OBD2 code then exists twice — once in
JavaScript, once in Swift — and has to stay together. Whoever corrects a byte
formula has to see both. In separate repositories they drift apart, and
unnoticed at that, because an error there only shows up at the car.

---

## The four building blocks

### 1. Network — the connection to jolt's API

Thin. A `URLSession` client, `Codable` structures for the responses, nothing
else. The domain calculation stays in the backend, where it is.

Access: `x-token` header, as expected by `deps.current_session`. The token comes
from `POST /api/auth/login` and belongs in the Keychain, not in `UserDefaults`.

What is needed to begin with:

| Purpose | Endpoint |
|---|---|
| Log in | `POST /api/auth/login`, `GET /api/auth/status` |
| Vehicles | `GET /api/vehicles`, `PUT /api/vehicles/{id}` |
| Calculate route | `POST /api/route` |
| Charging plan | `POST /api/trips/{id}/charge-plan` |
| Trip list | `GET /api/trips`, `GET /api/trips/{id}` |
| Start trip | `POST /api/live/start/{trip_id}` |
| Start recording | `POST /api/live/recording` |
| Report measurement point | `POST /api/live/{session_id}/point` |
| Read state | `GET /api/live/{session_id}` |
| Reload history | `GET /api/live/{session_id}/points` |
| Follow continuously | `WebSocket /api/live/{session_id}/ws` |

### 2. OBD — Bluetooth and protocol

The most laborious part and the one where the most can go wrong.

**CoreBluetooth** replaces the Bluefy bridge: find ELM327 adapters, connect,
find the serial characteristic, write commands, assemble responses into frames.
This is diligent work with known pitfalls (multi-frame responses, flow
control).

**The protocol** comes from `frontend/obd-core.js`. To be carried over are:

- the address blocks `BMS`, `CLIMATE`, `AKKU11`, `VEHICLE`, `DCDC` with their
  `cp`/`sh`/`cra`/`fcsh` values (they live in `frontend/readings.js`),
- the `READINGS` list with seventeen entries (`vals` in `frontend/readings.js`):
  data identifier, target address, byte formula, `required` flag,
- the protocol switching: the climate and 11-bit battery devices only answer
  under `ATSP6`, everything else under `ATSP7`. This is exactly what made
  **all** temperature values fail in a test drive, and the cause was the frame
  width, not the address.

**This translation is checked before it gets to the car.** Signs, scaling and
byte order are exactly the places where a port silently goes wrong — `soc_raw`
is one byte divided by 2.5, the battery current `(raw − 150000)/100` over four
bytes, and both would still look plausible even if wrong. `JoltTests` therefore
gets the recorded raw responses from real trips as test cases: the same hex
response in, the same numeric value out as in JavaScript.

### 3. Trip — recording in the background

The reason the whole thing has to be native.

`CLLocationManager` with `allowsBackgroundLocationUpdates = true` and the
"Always Allow" permission. Plus `UIBackgroundModes: location` and
`bluetooth-central` in the `Info.plist`.

Two things the PWA could not do and which work here without any fuss:

- **Keeping the screen awake** is `UIApplication.shared.isIdleTimerDisabled =
  true`. The video hack in `live.js` is dropped without replacement.
- **No data loss when the connection drops.** Measurement points go into a local
  queue first and from there to the backend. The trip of 4 Sep spent 46 % of its
  duration in gaps, and the largest part of that was dead spots, not sensor
  failure — the points existed, they just never arrived.

**There are two ways to report, and the second is the better one:**

`POST /api/live/{session_id}/point` needs the session ID and a token.

`POST /api/live/report` needs only the **vehicle's logger token** (from
`POST /api/vehicles/{id}/logger-token`). The backend finds the running session
itself, and if none is running, it answers with `200` and `recorded: false`
instead of an error — explicitly meant for a device that sends unattended.
Exactly the situation a background process is in. The token stays valid, while
session IDs change with every trip; a background service would otherwise have to
maintain state that it has long lost by the time it wakes up.

### 4. CarPlay — what is possible, and by which route

*Source: Apple's CarPlay Developer Guide, as of 8 Jun 2026
(<https://developer.apple.com/download/files/CarPlay-Developer-Guide.pdf>).
What is not stated there is marked below as an assumption or as open.*

**The dashboard itself cannot be shown.** CarPlay apps consist of a fixed set of
templates that iOS draws; web content, custom maps and charts do not exist.
What is possible is a **native short version** — a glance at what you need to
know on the road —, and there are two routes for that, which do not exclude each
other.

| | Route A: Widget and Live Activity | Route B: CarPlay app with templates |
|---|---|---|
| Approval from Apple | **none** ("Your app does not need to be a CarPlay app") | application with justification, review by Apple |
| Minimum | iOS 26 | iOS 14 (Driving task) or 16 (EV charging) |
| Where it appears | left of the CarPlay dashboard (widget), in the dashboard or as a notification (Live Activity) | own icon on the CarPlay home screen |
| What it can do | a glance: a few numbers, updated by the app | lists, information pages, grids; selection and buttons |
| Sizes | widget `systemSmall`, Live Activity `small` (the same as Apple Watch) | fixed templates, at most 2 to 3 levels deep (Driving task) |
| Swift target | widget extension (own target in the Xcode project) | scene delegate in the app itself |
| Effort | medium | higher, and it begins with waiting for Apple |

**Route A in detail.**

- A widget in CarPlay needs the family `.systemSmall`, a Live Activity
  `.supplementalActivityFamilies([.small])`. If the small activity is missing,
  CarPlay shows the compact views of the Dynamic Island.
- A widget does **not** open the app in CarPlay as long as the app is not a
  CarPlay app. It shows, it does not operate.
- A widget whose data sits behind data protection class A or B is useless in
  CarPlay: the iPhone is usually locked there. The data belongs in an
  unprotected store (class C or none) — a detail on which this easily fails.
- The Live Activity is the better carrier for jolt: it has a beginning and an
  end — the trip — and is updated by the app, which is running anyway
  (background location, dongle).

**Route B in detail.**

- Category **Driving task** (`com.apple.developer.carplay-driving-task`) or
  **EV charging** (`com.apple.developer.carplay-charging`, iOS 16). An app gets
  one category; it is chosen in the application. **Navigation** is ruled out —
  it requires turn-by-turn directions (`com.apple.developer.carplay-maps`).
- Driving task: tasks that "really help while driving"; templates only (no
  custom map); update data at most **every 10 seconds**; no location search; no
  use outside the vehicle.
- EV charging: must do more than a list of charging stations; on a map only
  charging stations may appear; up to five levels deep.
- For all: nothing may ask the user to pick up the iPhone, every flow must be
  possible without the iPhone, nothing unrelated (settings, account).
- **Which templates exist** (table in the guide, page 14, read as an image — in
  the text the check marks cannot be made out):

  | Template | Driving task | EV charging |
  |---|---|---|
  | Information (a few lines, buttons) | yes | yes |
  | List, grid (up to eight entries), tab bar | yes | yes |
  | Point of interest (places) | yes | yes |
  | Alert, action sheet | yes | yes |
  | Search | no | iOS 27 |
  | Map | no | no (navigation only) |
  | Template depth | 2, from iOS 26.4: 3 | 5 |
  | Data update | at most every 10 s | no limit stated |

  For jolt, what is needed is enough **in both** categories: an information
  template ("next stop") and a list of the stops.
- **Recommendation: EV charging.** The core of jolt is planning charging stops;
  that is the category's purpose. In addition there are five instead of two to
  three levels (stop list → stop → fallback site), no stated update limit and
  the template for places for "nearest charging station". The condition is that
  the app does "more than a list of charging stations" and shows only charging
  stations on maps — both apply. **The risk:** Apple decides after the
  application, and a later change of category would presumably mean a new
  application (assumption, not backed by the guide). Driving task would be the
  fallback, with tighter limits.
- Process: application at developer.apple.com/carplay, accept the additional
  agreement, Apple reviews and assigns the entitlement to the developer account,
  then a new provisioning profile with the CarPlay capability.

*Struck:* It used to say here that the status display would be "reviewed less
strictly than a navigation display". That is stated nowhere in the guide and was
an assumption.

#### What is displayed

The same information for both routes — all of it is already in the state
delivered by `GET /api/live/{session_id}` and the WebSocket:

| Display | Field in the state | Note |
|---|---|---|
| Charge level | `actual_soc`, `soc_source` | mark as "last measured" if not fresh |
| Range to reserve | `reserve_at_km` | empty if the destination is reached without recharging |
| Next charging stop | `next_stop` (name, km, arrival charge level) | only on a planned trip |
| Arrival | `arrival_shift_min` | "on plan", "+12 min" |
| Remaining | `remaining_km` | |

Traffic is **not** part of it: it appears only in the planning response and is
not stored (TomTom terms), so not in the running state either.

For a **recording** without a plan, the charge level and, if the car answers,
vehicle values remain — no charging stop, no arrival. The display has to cope
with missing fields; it shows what is there and invents nothing.

#### How the state gets to the Swift code

The state lives in the interface's JavaScript (`live.js: showState`), and that
runs as long as the app is kept alive — also with the phone locked (background
location, confirmed on a real trip). A small Capacitor plugin takes **a display
model** from there and passes it on to ActivityKit (route A) or to the template
(route B). No second route to the server, no second token, no second
calculation.

The display model is a pure function `state → {a few numbers and texts}` and can
be built and tested **without Swift and without a Mac**: rounding, placeholders
for missing values, "stale" after a deadline, at most one update every 10
seconds (the rule of route B; route A has its own limits, see below).

#### How Swift code gets into a generated project

The iOS project is **generated** in CI and not checked in (see "Building and
delivery") — that is meant to stay that way. The Swift code is therefore
checked in next to the configuration and added after `cap add ios`:

- **Plugin and CarPlay scene (route B):** as a local Swift package (like the
  community plugins), which `cap sync` pulls in. The scene delegate is listed in
  the `Info.plist` by class name with module name; `tools/ios_info_plist.sh`
  adds the scene manifest, the entitlement goes in as a build setting
  (`CODE_SIGN_ENTITLEMENTS`). That hardly touches the Xcode project.
  **Assumptions, unchecked:** that a scene delegate from a Swift package can be
  wired into the `Info.plist` by its module name, and that a CarPlay scene
  manifest also requires the iPhone window to be run as a scene. Whether the
  Capacitor template already does this or would have to be changed is open — and
  would be the actual intervention in the app.
- **Widget extension (route A):** a widget is a **target of its own** with its
  own bundle identifier — not representable as a package. The Xcode project has
  to be extended programmatically (the Ruby tool `xcodeproj` or XcodeGen on the
  macOS runner; `tools/ios_signatur.sh` already patches the project today).
  **Open:** the extension needs its own app ID and its own profile. Whether
  automatic signing via the API key (`-allowProvisioningUpdates`) creates that
  for a second target without manual work is **not checked** and decides how
  much of route A runs in CI. Only an attempt will show.

`tools/check_ios.py` gets the checks that can be made without a Mac: scene
manifest present, entitlement set, the widget target in the project.

#### What cannot be checked without a device

As in the whole app: CI answers whether it **builds**. Whether it **appears** in
the car is shown only by CarPlay — on the Mac in the *CarPlay Simulator* (part
of the additional Xcode tools) or in the vehicle via TestFlight. A Mac is
missing; so the car remains the test, and the first attempt there is an attempt.

Not looked up, but to be clarified before building: how long a Live Activity may
run and how often it can be updated (Apple limits both) — on an eight-hour trip
that is no side issue.

---

## Order

Every step ends with something that runs.

Steps 1 to 6 above describe the SwiftUI route. The Capacitor stage is built
first, because it takes the same three limits without touching the byte
formulas.

| # | Step | Result | Status |
|---|---|---|---|
| 1 | Capacitor scaffolding, plugin shell, `bt()` in the core | Bluetooth runs natively instead of via Bluefy | done |
| 2 | `keep-awake` instead of the video workaround | screen stays on | done |
| 3 | CI generates and builds the iOS project | "does it compile" answerable without a Mac | built; the first run failed on the app ID (hyphen), fixed |
| 4 | Apple Developer Program, signing, TestFlight | app gets onto the iPhone | workflow done (`ios-testflight.yml`), waiting for account and secrets — instructions: [`ios-einrichten.md`](ios-einrichten.md) |
| 5 | Background location via plugin | recording with the screen locked | built in (`@capacitor-community/background-geolocation`), **unchecked on the device** |
| 6 | Queue against dead spots | no more gaps | done (`live.js`, batch endpoint `/points`) |
| 7 | Display model: state → a few numbers (`live.js`), with test | the basis for both routes, without Swift and without Apple | **done:** `frontend/display.js` (pure function, throttled sender) and `tools/check_display.js`; `live.js` reports state and end of trip, a plugin later only sets the target |
| 8 | CarPlay application to Apple (choose category, check templates in the guide) | entitlement for route B | **draft done:** [`carplay-antrag.md`](carplay-antrag.md) (category EV charging, English text to paste in); you submit it yourself |
| 9 | Route A: plugin, Live Activity, widget target in CI, signing | charge level and next stop in the CarPlay dashboard | **built, unchecked on the device:** plugin `plugins/jolt-anzeige` (ActivityKit), widget extension `ios-native/JoltWidget` (family `.small`), `tools/ios_widget.sh/.rb` attaches the target after `cap add ios`, `check_ios.py` checks the model against Swift fields. **Open:** whether automatic signing creates the extension's identifier and profile (`<App-ID>.widget`) itself on export; whether CarPlay shows the activity. Apple ends a Live Activity after 8 hours. No `systemSmall` widget (only the Live Activity). The extension applies from iOS 18 (small CarPlay family; `if #available` in the `WidgetBundle` does not build), the app keeps its minimum version (raising it made `cap sync` write an invalid `Package.swift`). **Unchecked:** whether App Store Connect accepts an extension with a higher minimum version than the app. |
| 10 | Route B: CarPlay scene with templates | list of charging stops in the car | **built, unchecked in the car:** entitlement `carplay-charging` has been granted (October 2026). `JoltCarPlaySceneDelegate` (lists "Jetzt" and "Ladestopps", stop page), `JoltAnzeigeStore` as bridge from the plugin, `tools/ios_carplay.sh/.rb` (second scene in the Info.plist, AppDelegate, entitlement). **Open:** enable the entitlement in the portal for the app ID and set `CARPLAY_ENTITLEMENT` ([`ios-einrichten.md`](ios-einrichten.md)); whether the scene shows anything on a cold start without the interface (then only the stored last state); **starting and ending a recording** works from the list (start with the most recently used vehicle, end with a confirmation prompt) — but only if the interface is running; on a cold start without it, CarPlay says that jolt has to be opened on the iPhone. A start with a locked iPhone may fail because of location (the interface asks for it at the start); the reason then appears in CarPlay. A vehicle choice in CarPlay is still missing. |

**Step 4 is the bottleneck, not the code.** Everything up to and including 3
runs without an Apple account and without a Mac. From 4 on, nothing works
without the Developer Program: without signing there is no way onto a device,
and without a Mac or stored certificates no signed build.

CarPlay comes last, but **no longer behind SwiftUI**: both routes go through
Capacitor with a small plugin, the app stays as it is. The application (8) and
the display model (7) depend on nothing and can begin immediately; 9 and 10
need a device with iOS 26 or a vehicle, respectively.

**Recommended order:** 7 and 8 at the same time, then 9. Route A brings
something into the car without an application and without waiting; route B is
the added value for the list of stops and is worthwhile if it is needed on the
road.

---

## Building and delivery

**There is no Xcode and no Mac.** That is the condition under which everything
here stands, and the Capacitor route copes with it: the iOS project is not
maintained by hand but generated on every run from `package.json` and
`capacitor.config.json`. That is why `ios/` is not checked in either — it would
be a second source of truth next to the configuration, and the two would drift
apart unnoticed.

**Building automatically** via GitHub Actions: `.github/workflows/ios.yml`
creates the project with `cap add ios`, adds to the `Info.plist` via
`tools/ios_info_plist.sh` and builds against the simulator, without signing.

The path filter is narrow: `frontend/**` is **not** in it. The app loads its
interface at runtime from the server (`server.url` in `capacitor.config.json`),
so a changed line in `live.js` needs no new app build but goes the usual way via
the container. This is exactly what the setup was chosen for — the fast deploy
route stays.

The JavaScript-side checks, on the other hand, run on every commit in `ci.yml`,
on a Linux runner and thus without a minute multiplier:
`tools/check_ble_bridge.js` plays through the path of a round at the car against
a simulated dongle, and a comparison makes sure that `frontend/ble-plugin.js`
still matches its entry.

Runs on `macos-latest`, and for private repositories a **minute multiplier of
10** applies there — one minute on a Mac runner counts as ten against the quota.
That is why the workflow only builds against the simulator and does not sign: a
simulator build needs neither certificate nor provisioning profile and is the
cheapest way to answer "does it still compile at all".

**The app gets onto devices via TestFlight**, and without a Mac there is no way
around it: the usual substitute — device on the cable, Xcode trusts it, app runs
for seven days — presupposes exactly the Xcode that is missing here.

**An Apple Developer Program at $99/year is therefore mandatory**, and earlier
than on the SwiftUI route. What is to be done after that can be done entirely
without a Mac; the steps, with commands, are in
[`ios-einrichten.md`](ios-einrichten.md):

1. Distribution certificate — the signing request (CSR) is generated by
   `openssl`.
2. App ID `de.thesmarthome.jolt` (without a hyphen: Capacitor rejects it), the
   app in App Store Connect and an API key for the upload.
3. API key (role Admin) and team ID as four repository secrets; Apple creates
   certificate and profile itself. Anyone who wants to sign manually adds three
   more.

The workflow behind it is done: `.github/workflows/ios-testflight.yml` archives
(automatically signed, manually as a fallback) and uploads directly to
TestFlight, triggered by hand or by the tag `ios-*`. `tools/ios_signatur.sh`
only switches the app target for this (a command-line setting would also hit the
Swift packages, which do not know profiles), `tools/ios_symbol.sh` sets the jolt
icon instead of the Capacitor logo, `tools/check_ios.py` checks in the normal CI
everything that can be checked without a Mac.

Only after that is the app on the phone. Until then, CI only answers whether it
can be built — which is not little, but nothing drives yet.

---

## What this document does not settle

- **How much of the web interface disappears later.** Sensible to decide only
  once the app holds up in daily use.
- **Whether the iPad is suitable as a test device.** Only the cellular models
  have a real GPS receiver; Wi-Fi-only models estimate the position and are
  useless for recording a trip.
- **Whether Apple grants the CarPlay entitlement**, and for which category
  (Driving task or EV charging; an app gets one). Only route B depends on it,
  route A does not.
- ~~Whether the vehicle has CarPlay and which iOS version the iPhone has.~~
  **Answered (5 Oct 2026): wireless CarPlay, latest iOS version.** Route A is
  therefore possible. Wireless means that the iPhone stays in the pocket and
  locked — exactly the case in which widget data behind data protection class A
  or B shows nothing. Data and display therefore belong in an unprotected store.
- **Whether the Capacitor template already runs the iPhone window as a scene**,
  as a CarPlay scene manifest presupposes (route B).
- **Whether automatic signing via the API key creates a second app ID for the
  widget extension itself.** Not checked; an attempt in CI will settle it.
- **How long a Live Activity runs on a long trip.** Not looked up.
