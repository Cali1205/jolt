# jolt — route planner for electric cars

jolt plans charging stops for an electric car and **updates the plan while you
drive**. A plan calculated at departure is wrong after eighty kilometres:
speed, temperature, wind and traffic all add up in the same direction. jolt
measures what the car really uses, through the phone and an OBD2 dongle, and
replans the rest of the trip when it matters.

It is a self-hosted web app: a FastAPI server with Postgres, and a
progressive web app for the phone. There is an optional iPhone app with
CarPlay.

> **Status.** A personal project, used daily on a VW ID.Buzz. The app's
> interface is **German only**; the code and this documentation are English.
> Cars other than VW MEB models are supported through platform profiles that
> have not been tried on a car yet ([docs/obd.md](docs/obd.md)).

## Features

- **Consumption from physics,** not a flat kWh/100 km: air drag with v²,
  gradient from the elevation profile, recuperation, heating by time, weather
  per waypoint at the hour you get there.
- **Charging stops** chosen for the shortest total time, with the charging
  curve of the car, a fallback charger for every stop, and costs in euros.
- **Replanning while driving** when the next charger is occupied, the reserve
  is in danger, you leave the route, or the arrival time or charge level drift
  away from the plan. A message reaches the phone even with the screen off.
- **Recording trips** with GPS and the car's own data: odometer, energy
  counters, battery temperature, auxiliary loads. Works without network and
  catches up later.
- **Learns your car:** a correction factor per vehicle from every recorded
  trip, and the battery capacity the car reports instead of the brochure
  figure.
- **OBD2 dongle via Bluetooth,** directly from the browser (Web Bluetooth) or
  the iOS app. Platform profiles for VW MEB, Stellantis e-CMP (Opel Corsa-e,
  Peugeot e-208) and standard OBD2.
- **Chargers** from the German Bundesnetzagentur register and Open Charge Map.
  Routes from openrouteservice; TomTom optionally adds alternatives and
  traffic.
- **CarPlay** dashboard and Live Activity in the iOS app.

Each feature and the reasoning behind it: [docs/features.md](docs/features.md).

## Quick start

Try it on your computer, with demo routes and SQLite:

```bash
pip install -r backend/requirements.txt
cd backend && python -m uvicorn app.main:app --port 8322
```

Open <http://localhost:8322>. Without an `ORS_API_KEY` the routes are straight
lines, and the app says so.

Run it for real, with Docker:

```bash
git clone https://github.com/Cali1205/jolt.git && cd jolt
cp .env.example .env    # set DB_PASSWORD, APP_PASSWORT and ORS_API_KEY
docker compose up -d    # app on http://127.0.0.1:8322
```

On a phone you need HTTPS: location, Bluetooth and notifications are only
allowed on secure pages. **[docs/deploy.md](docs/deploy.md)** walks through
the whole setup: HTTPS with a Cloudflare Tunnel or your own reverse proxy,
importing chargers, notifications, updates, backups and a security
checklist.

## Using jolt

The app has five tabs:

| Tab | What it is for |
|---|---|
| **Planen** (plan) | Start, destination, departure time, speed and charge level; then the route variants with charging stops and the range on the map. |
| **Live** | The running trip: actual against plan, consumption, the car's values, replanning. |
| **Fahrten** (trips) | Planned and recorded trips. "Aufzeichnung starten" records a trip without a plan. |
| **Fahrzeug** (vehicle) | Vehicle profiles: battery, mass, drag, charging curve, OBD2 platform, logger token. Start from a template. |
| **Einstellungen** (settings) | Login, notifications, dongle options and diagnostics, storage. |

A typical first day:

1. Create your vehicle in **Fahrzeug** from the closest template. Set the net
   battery capacity and, if you have a dongle, the OBD2 platform.
2. Plan a trip in **Planen** and compare the variants.
3. In the car, start the trip, or record one under **Fahrten**. With a dongle
   the app reads the car while you drive; without one it uses GPS and asks for
   the charge level.
4. After a few recorded trips the correction factor in the vehicle profile
   reflects how your car really drives.

## Documentation

| Document | Content |
|---|---|
| [docs/deploy.md](docs/deploy.md) | Running jolt on your own server |
| [docs/obd.md](docs/obd.md) | Dongle, supported cars, adding a platform, in-car logger |
| [docs/obd-meb.md](docs/obd-meb.md) | VW MEB data identifiers and where they come from |
| [docs/features.md](docs/features.md) | Every feature in detail |
| [docs/ios-app.md](docs/ios-app.md) | Building the iPhone app (TestFlight, without a Mac) |
| [docs/development.md](docs/development.md) | Local setup, checks, code structure, next steps |
| [docs/concept.md](docs/concept.md) | The original concept and its reasoning |
| [docs/ios-concept.md](docs/ios-concept.md) | Design of the iOS app |
| [docs/carplay-application.md](docs/carplay-application.md) | Draft of the CarPlay entitlement request |

## Configuration

All settings are environment variables, set in `.env` (see `.env.example`).

| Variable | Meaning |
|---|---|
| `DB_PASSWORD` | Postgres password. Required in Docker. |
| `APP_PASSWORT` | Login password. **Empty means no login**; the log warns about it. |
| `ORS_API_KEY` | openrouteservice. Required in Docker; without it, demo routes. |
| `TOMTOM_API_KEY` | Optional: alternative routes and traffic. Nothing is stored. |
| `OCM_API_KEY` | Optional: Open Charge Map import. |
| `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT` | Push notifications; generate with `tools/push_keyname.py`. |
| `JOLT_DATA` | Directory for database and backups (default `./data`). |
| `JOLT_BIND`, `JOLT_PORT` | Where the app is published (default `127.0.0.1:8322`). |
| `TRUSTED_PROXIES` | Addresses or networks of a reverse proxy or tunnel that may set `X-Forwarded-For`. |
| `TUNNEL_TOKEN` | Cloudflare Tunnel token for `docker-compose.tunnel.yml`. |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.tunnel.yml` to always include the tunnel. |
| `COMPOSE_PROFILES` | `autoupdate` starts Watchtower for automatic updates. |
| `RATE_LIMIT_PER_MIN` | Requests per minute and client (default 120). |
| `LOGIN_LIMIT_PER_15MIN` | Failed logins per 15 minutes and client (default 10). |
| `POINTS_LIMIT_PER_MIN`, `WS_LIMIT_PER_MIN` | Measurement uploads per minute (600) and new WebSocket connections per minute (30), per client. |
| `BACKUP_KEEP_DAYS`, `TZ` | Backup retention (14 days) and time zone. |
| `DATABASE_URL` | Set by Compose; without it the app uses SQLite (`jolt_dev.db`). |
| `ENABLE_API_DOCS` | `1` enables `/api/docs`. Leave empty in production. |

## Data sources and credits

- Routes and elevation: [openrouteservice](https://openrouteservice.org).
  Traffic and alternatives: [TomTom](https://developer.tomtom.com) (optional).
- Weather: [Open-Meteo](https://open-meteo.com). Map tiles:
  [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
- Chargers: [Bundesnetzagentur](https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html)
  and [Open Charge Map](https://openchargemap.org).
- Vehicle data identifiers: [spot2000](https://github.com/spot2000/Volkswagen-MEB-EV-CAN-parameters),
  [WiCAN](https://github.com/meatpiHQ/wican-fw),
  [codingABI](https://github.com/codingABI/id3esp32obd2),
  [OBDb](https://github.com/OBDb) (CC BY-SA 4.0) and
  [OVMS](https://github.com/openvehicles/Open-Vehicle-Monitoring-System-3).

Reading a car's diagnostics is at your own risk. jolt only sends read
requests, but a dongle left in the socket can drain the 12 V battery, and
asking a locked car can set off its alarm ([docs/obd.md](docs/obd.md)).
