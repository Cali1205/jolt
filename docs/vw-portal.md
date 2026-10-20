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

On failure jolt waits longer before the next try: twice the interval after
each failure, at most two hours.

The API behind it, for your own scripts:
`GET /api/vehicles/{id}/snapshot` (newest stored report, 404 if none) and
`GET /api/cloud/status` (whether the module is on and how fetching goes).

## Privacy

- The VW ID and password are only in `.env`. jolt never logs them and never
  returns them through the API. The VIN appears only shortened to its last
  four characters.
- jolt stores the values it knows (the list above) per vehicle, one row per
  report of the car, in its own database. Data points it does not use are not
  stored.
- jolt talks only to `identity.vwgroup.io` and
  `eu-data-act.drivesomethinggreater.com`, and refuses redirects to any other
  host during the login.
- To stop, remove the two variables and restart. Stored reports stay until
  you delete the vehicle.
