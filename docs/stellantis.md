# Stellantis: Opel, Peugeot, Citroën, DS

Cars of the Stellantis brands report their state to the brand's cloud: the
same data the MyOpel, MyPeugeot, MyCitroën and MyDS apps show. jolt can read
it with the owner's login and use it where no dongle reading exists. The
module is optional and off until you log in once.

The client is ported from [evcc](https://github.com/evcc-io/evcc) (MIT
license, Copyright (c) evcc.io), the field meanings follow
[psa_car_controller](https://github.com/flobz/psa_car_controller).

## What jolt gets

| Signal | From the car |
|---|---|
| `soc_pct` | Charge level |
| `range_km` | The car's range estimate |
| `odometer_km` | Odometer |
| `plugged` | Cable plugged in |
| `charge_state` | Charging, ready, not ready, target reached, error |
| `charge_type` | AC or DC, while charging |
| `charge_remaining_min` | Remaining charge time, while charging |
| `battery_health_pct` | State of health, when the car reports it |
| `climate_state` | Preconditioning on or off |
| `lat`, `lon`, `position_at` | The car's last position and when it was fixed |

The names are jolt's signal catalog ([data-model.md](data-model.md)). The
car reports no charge power, no battery temperature and no speed, so the
card "Daten" in the tab "Fahrzeug" shows those as missing unless a dongle
delivers them.

## Limits

- **Not live.** jolt asks every `STELLANTIS_INTERVAL_MIN` minutes (default
  15), and once an hour while nothing changes. The values are as fresh as
  the car's last report.
- **Read-only.** jolt starts neither charging nor preconditioning.
- **No charge power.** Charges in the logbook come from the charge state;
  their energy is the rise of the charge level times the usable capacity,
  and they have no power curve. A stop shorter than two polls (30 minutes by
  default) does not split a drive.
- **The login cannot be automated.** The brand's login page has a captcha
  and ends on an address only the brand app can open. You log in once in
  your browser and copy a code; after that jolt refreshes the tokens itself.
- **It can break without notice.** Stellantis has changed the API and the
  login before. When it breaks, the settings show the error and jolt keeps
  working without the data.

## One-time login

1. Add to `.env` next to `docker-compose.yml`:

   ```sh
   STELLANTIS_BRAND=opel            # opel, peugeot, citroen or ds
   STELLANTIS_USER=you@example.org  # the brand account's e-mail
   STELLANTIS_COUNTRY=de            # the account's country
   ```

2. Run the login tool. It needs the database running, as usual:

   ```sh
   docker compose run --rm --no-deps -it jolt-app python tools/stellantis_login.py
   ```

3. The tool prints an address. In a desktop browser, open the developer
   tools first (F12, tab "Network" / "Netzwerk"), then open the address and
   log in with the brand account.
4. At the end the browser tries to open an address like
   `mymopsdk://oauth2redirect/de?code=...` (Opel; Peugeot uses `mymap://`,
   Citroën `mymacsdk://`, DS `mymdssdk://`). It cannot, and shows an error
   or seems to do nothing. That is expected. Find that address in the
   address bar or in the network tab (the last request, header `Location`).
5. Copy the code, or the whole address, into the tool. A code works once and
   only for a few minutes; if it is refused, start again at step 2.
6. The tool stores the tokens in the database and lists the account's
   vehicles (VIN shortened). Restart the app, not during a recording:

   ```sh
   docker compose up -d --force-recreate jolt-app
   ```

The log shows `stellantis: fetching every 15 min` and, after the first
success, `stellantis: first snapshot fetched`.

Optional:

| Variable | Meaning |
|---|---|
| `STELLANTIS_VIN` | The vehicle in the account. Only needed when it has more than one. |
| `STELLANTIS_VEHICLE` | The jolt vehicle id the data belongs to. Needed as soon as jolt has more than one vehicle, for example an ID.Buzz on the VW portal and a Corsa here. |
| `STELLANTIS_INTERVAL_MIN` | Minutes between fetches. Default 15, at least 5. |

### Do not reuse evcc's or another tool's tokens

Every token refresh returns a new refresh token and invalidates the old one.
Two programs that share one refresh token log each other out: whichever
refreshes second gets `invalid_grant`. Give jolt its own login with the tool
above, even if evcc, Home Assistant or psa_car_controller already run with
the same account.

### When the login expires

When a refresh is refused (the account's password changed, the tokens were
used elsewhere, or Stellantis revoked them), the tab "Einstellungen" shows
`stellantis: token refused (invalid_grant) - log in again: ...` and jolt
asks less and less often. Run the login tool again and restart the app.

## What jolt does with the data

- **Tab "Fahrzeug":** the card "Stellantis" with charge level, range,
  odometer and charge state; the card "Daten" with what the car delivers.
- **Recording start:** without a dongle reading, jolt takes the charge level
  if the car reported it within the last 12 hours ("Startladestand 64 % aus
  der Stellantis-Cloud").
- **Logbook:** drives (from the odometer) and charges (from the charge
  state), with the source "Stellantis". A jolt recording of the same drive
  merges with it.
- **Tab "Einstellungen":** the row "Stellantis" shows whether the last fetch
  worked.

## Privacy

- The tokens are stored in the table `cloud_credentials`, under the brand
  and the account's e-mail. jolt never logs them and never returns them
  through the API. The VIN appears only shortened.
- jolt stores, per poll that brought a new report, the values above and the
  whole status answer, **including the car's position**. Over time that is a
  record of where the car was parked, every quarter hour. It stays in your
  database; delete the vehicle to remove it.
- jolt talks only to `idpcvs.<brand>.com` (`idpcvs.driveds.com` for DS) and
  `api.groupe-psa.com`.
- To stop, remove `STELLANTIS_USER` and restart. To also drop the tokens:
  `DELETE FROM cloud_credentials WHERE source = 'stellantis';`
