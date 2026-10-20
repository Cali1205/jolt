# The VW EU Data Act portal

Volkswagen group cars report their state to VW. Under the EU Data Act, VW
gives owners access to that data through a web portal:
<https://eu-data-act.drivesomethinggreater.com>. jolt can log in there, fetch
the car's last reported values and use them where no dongle reading exists.
This module is optional and off unless you configure it.

VW closed the WeConnect app API to third parties in May 2026. The portal is
the way that still works. The login code is ported from
[evcc](https://github.com/evcc-io/evcc) (MIT license, Copyright (c) evcc.io).

## Limits

- **Not live.** The values are as fresh as the car's last report. A parked,
  sleeping car reports rarely; the portal keeps a new dataset about every 15
  minutes while the car reports.
- **Read-only.** jolt cannot start charging, climatisation or anything else.
- **It can break without notice.** VW changes the login now and then: it
  happened in May and October 2026. When it breaks, the settings show the
  error and jolt keeps working without the portal data.
- **One car per jolt installation.** The data goes to one jolt vehicle.

## One-time setup in the browser

Do this once, in a normal browser, with the VW ID you use in the VW app.

1. Open <https://eu-data-act.drivesomethinggreater.com> and log in with your
   VW ID.
2. Accept the terms and the consent the portal asks for.
3. Link your vehicle, if the portal does not list it yet. The vehicle must be
   in your VW ID account.
4. Create a data request for the vehicle and switch on the **continuous data
   request** (a new dataset every 15 minutes).
5. Wait until the portal lists the first dataset. That can take a while; the
   car has to report first.

The labels in the portal can differ from the ones above. If the probe below
reports "confirm the consent", the portal still wants something from step 2
or 3. If it reports "no data request", step 4 is missing.

## Configure jolt

Add to `.env` next to `docker-compose.yml`:

```sh
VW_PORTAL_USER=you@example.org
VW_PORTAL_PASSWORD=your-vw-id-password
```

Optional:

| Variable | Meaning |
|---|---|
| `VW_PORTAL_VIN` | The vehicle in the VW account. Only needed when the account has more than one. |
| `VW_PORTAL_VEHICLE` | The jolt vehicle id the data belongs to. Only needed when jolt has more than one vehicle. |
| `VW_PORTAL_INTERVAL_MIN` | Minutes between fetches. Default 15, at least 5. |
| `VW_PORTAL_USER_AGENT` | Replaces the `User-Agent` jolt sends. Only if the login is refused with 403/406: VW's firewall decides partly by headers. |

Test the login before you restart jolt:

```sh
docker compose run --rm --no-deps jolt-app python tools/vw_portal_probe.py
```

The probe logs in, lists the vehicles of the account (only the last four
characters of the VIN), and prints the values jolt would store with their
age. It stores nothing. Exit code 0 means it works.

Then restart the app:

```sh
docker compose up -d jolt-app
```

The log shows `vw_portal: fetching every 15 min` and, after the first
success, `vw_portal: first snapshot fetched`. After that it logs only when
something changes: an error, a different error, or working again.

## What jolt does with the data

- **Tab "Fahrzeug":** a card "VW-Portal" with charge level, range, odometer,
  battery energy, battery temperature, charge state and the time of the
  car's report.
- **Recording start:** when the dongle gives no charge level, jolt takes the
  portal's charge level if the car reported it within the last 12 hours, and
  says so in the message ("Startladestand 57 % aus dem VW-Portal (Stand
  02:17)"). A dongle reading always wins.
- **Tab "Einstellungen":** the row "VW-Portal" shows whether the last fetch
  worked, the last error and the time of the last success.

jolt asks every `VW_PORTAL_INTERVAL_MIN` minutes while the car has reported
within the last two hours, and once an hour while it has been quiet longer.
Nothing is missed by asking less often: every report the car made in between
is still listed in the portal, and jolt stores each one as its own row, so
parked, driving and parked again stay three moments instead of one. On
failure jolt waits longer before the next try: twice the interval after each
failure, at most two hours.

The API behind it, for your own scripts:
`GET /api/vehicles/{id}/snapshot` (newest stored report, 404 if none) and
`GET /api/cloud/status` (whether the module is on and how fetching goes).

## Logbook: drives and charges

jolt builds a logbook of drives and charges, shown in the tab "Fahrten"
under "Fahrtenbuch". Three sources feed it, and none needs the others:

- **The history export** of the portal (one-off import, see below).
- **The portal's live reports**, while the module is on. jolt stores a drive
  or charge from them 30 minutes after it ended: the car sends its
  switch-off reports late and out of order, and an ongoing charge must not
  be stored as finished.
- **jolt's own recordings.** Each finished live session becomes a drive,
  also without VW credentials.

When two sources saw the same drive, it is one entry with both names
("VW + jolt"): entries match by overlapping odometer ranges, or by time
where one side has no odometer. Times, odometer and charge level come from
the finest source (history export, then portal, then jolt), the energy from
the dongle, which reads the battery's own counters. Storing the same data
again changes nothing.

How drives and charges are found, with the thresholds and why, is described
at the top of `backend/app/cloud/sessions.py`. In short: a drive is a run of
odometer increases, and ten minutes without a new kilometre, charging or
plugging in end it. A charge is a run of reports with charge power above
zero; pauses under 15 minutes stay inside it, and its energy is the power
integrated over time.

What the VW data does **not** contain: positions. A drive that only VW saw
has no map and no route; only drives jolt recorded link to the trip view.
Consumption in kWh/100 km is known only where the dongle counted along.

API: `GET /api/vehicles/{id}/drives` and `GET /api/vehicles/{id}/charges`,
newest first, `limit` (default 30, at most 200) and `before` (an ISO time,
for the next page), each with totals.

## Import the history

In the portal you can request the car's whole history once. You get a zip
with one JSON file (about 70 MB for a year). jolt reads it, finds the
drives and charges and stores those with their kilometre, charge level and
power tracks. It does **not** keep the raw data points: that would be half
a million rows for a year, and nothing in jolt reads them.

Try it first without writing anything:

```sh
docker compose run --rm --no-deps -v "$PWD/export.zip:/tmp/export.zip:ro"     jolt-app python tools/import_vw_history.py /tmp/export.zip --dry-run
```

Then without `--dry-run`. With more than one vehicle in jolt, add
`--vehicle ID`. The import also adds every finished jolt recording to the
logbook, so drives both saw become one entry. Importing the same file
again, or a newer export that overlaps it, changes nothing that is already
there. The summary shows the period, the number of drives and kilometres,
the charges with kWh split into AC and DC, and how many drives were merged
with jolt recordings.

The file contains the VIN and your VW user id. jolt prints the VIN only
shortened and stores neither.

## Privacy

- The VW ID and password are only in `.env`. jolt never logs them and never
  returns them through the API. The VIN appears only shortened to its last
  four characters.
- jolt stores per vehicle, one row per report of the car, the values it
  knows (the list above) and every data point that report carried, also the
  ones jolt does not interpret yet. That depends on which data categories
  you released in the portal.
- jolt talks only to `identity.vwgroup.io` and
  `eu-data-act.drivesomethinggreater.com`, and refuses redirects to any other
  host during the login.
- To stop, remove the two variables and restart. Stored reports, drives and
  charges stay until you delete the vehicle.
