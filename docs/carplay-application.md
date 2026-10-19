# CarPlay application for jolt (draft)

Step 8 of [`konzept-ios-app.md`](konzept-ios-app.md). The application is for
**route B** (own CarPlay app with templates). Route A, the Live Activity on the
CarPlay dashboard, needs no application and is already running.

Source for categories and rules: Apple's *CarPlay Developer Guide* (as of
8 June 2026). Whatever is not stated there is marked **open** below.

## What you have to do

1. Submit the application at <https://developer.apple.com/carplay/> (section
   "Request CarPlay entitlement"). It needs the account under which the app is
   in App Store Connect; the Team ID is in `APPLE_TEAM_ID`.
2. Choose the category **EV charging** (entitlement
   `com.apple.developer.carplay-charging`). Fallback: **Driving task**.
3. Paste the English text below into the description field. I wrote down only
   what jolt can do today or what this application explicitly names as a plan.
4. Apple reviews it and assigns the entitlement to the account. After that the
   provisioning profile needs the CarPlay capability; with automatic signing
   (`-allowProvisioningUpdates`) Apple creates it during export, but this is
   **untested**.

## Decision: EV charging or Driving task

| | EV charging | Driving task |
|---|---|---|
| Fit for jolt | The core is planning charging stops | Only "tasks that help while driving" |
| Template depth | 5 levels | 2, from iOS 26.4: 3 |
| Update rate | no limit stated | at most every 10 s |
| Condition | App must do more than a list of charging stations; maps may show only charging stations | no map, no place search |
| Risk | Apple rejects it if it looks like a plain charging-station list | narrower, but uncritical |

Recommendation: **EV charging.** jolt plans the route, calculates the state of
charge and chooses the charging stops; the charging stations are the result, not
the content. That is exactly the distinction the guide requires.

## What should appear in CarPlay (and what not)

All templates are fixed iOS templates; jolt draws nothing itself.

| Screen | Template | Content |
|---|---|---|
| Start | Information | State of charge, next charging stop (name, distance, expected state of charge on arrival), arrival shift relative to the plan |
| Stops | List | The planned charging stops of the current trip, with distance and state of charge on arrival |
| Stop | Information | Operator, power, connectors, charging time according to the plan |
| Alternatives | List / Point of Interest | Nearby alternatives if the planned stop cannot be reached |

**Not** in CarPlay: planning a new route (that happens before the drive on the
iPhone), settings, vehicle data, charts, account.

**Open:** Apple requires that "every flow is possible without the iPhone". Today
the trip is planned on the iPhone or the recording is started there. For CarPlay
alone to suffice, CarPlay would need a **selection of saved trips** (list). That
is not built and belongs in step 10; in the application it is stated as a plan.

## English text for the application form

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

## What I cannot know

- Whether Apple accepts an app that is not yet in the App Store and is used by a
  single household. The guide names no condition for this, but the review is
  Apple's decision. The "Distribution" paragraph is therefore kept honest and
  not embellished.
- Whether a later change of category needs a new application
  (assumption: yes).
- How long the review takes.

## Once the application is approved

1. Entitlement `com.apple.developer.carplay-charging` in the provisioning profile.
2. CarPlay scene (scene delegate) in the plugin, scene manifest via
   `tools/ios_info_plist.sh`, entitlement via `CODE_SIGN_ENTITLEMENTS`.
3. The templates are filled by the same display model (`frontend/display.js`)
   that already feeds the Live Activity.
4. Verify: in the CarPlay Simulator (Mac) or in the vehicle via TestFlight.
