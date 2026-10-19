# Deploying jolt

This guide takes you from an empty Linux host to jolt running on your own
domain over HTTPS. Plan for about half an hour.

## What you need

- **A Linux host with Docker** and the Compose plugin (`docker compose
  version`). A small VPS or a home server is enough: jolt and Postgres use
  well under 1 GB of memory. The ready-made image is built for x86-64
  (amd64) only; on an ARM host (Raspberry Pi, Ampere) run `docker compose
  build` once before `docker compose up -d`.
- **An HTTPS address.** The phone only allows location, Bluetooth and
  notifications on HTTPS pages, so plain `http://<ip>:8322` is fine for a look
  around but not for driving. Below are two ways: a Cloudflare Tunnel (no open
  port, no certificate to manage) or your own reverse proxy.
- **A free openrouteservice key** for routes and elevation:
  <https://openrouteservice.org/dev/#/signup>.

Optional: a TomTom key (alternative routes and traffic), an Open Charge Map
key (chargers outside Germany), and a Bluetooth OBD2 dongle ([obd.md](obd.md)).

## 1. Get the files

```bash
git clone https://github.com/Cali1205/jolt.git
cd jolt
cp .env.example .env
```

You only need `docker-compose.yml`, `docker-compose.tunnel.yml`,
`.env.example` and `tools/` on the host; the app itself comes as a ready-made
image, `ghcr.io/cali1205/jolt`. It is built from `main` whenever all checks
pass. If you would rather build it yourself, run `docker compose build`
before starting; for a fork, change `image:` in `docker-compose.yml` to your
own registry.

## 2. Fill in `.env`

Three values are required:

```bash
DB_PASSWORD=<random>        # openssl rand -base64 24
APP_PASSWORT=<random>       # the login password of the app
ORS_API_KEY=<your key>
```

`.env.example` explains every other setting. Keep `.env` readable only for
its owner: `chmod 600 .env`.

## 3. Start

```bash
docker compose up -d
docker compose ps                         # jolt-db healthy, jolt-app up
curl -s http://127.0.0.1:8322/api/status  # {"password_required":true,...}
```

This starts three containers: `jolt-db` (Postgres), `jolt-app` and
`jolt-backup` (a nightly database dump at 03:30). Data lives in `./data`, or
wherever `JOLT_DATA` points. Database migrations run when the app starts.

The app listens on `127.0.0.1:8322` only. That is deliberate: Docker publishes
ports past the host firewall (`ufw` does not see them), so `0.0.0.0` would put
jolt on the internet without HTTPS. To reach it from your LAN or a VPN, set
`JOLT_BIND` to that address.

## 4. HTTPS

### Option A: Cloudflare Tunnel (recommended)

A `cloudflared` container next to the app dials out to Cloudflare, and
Cloudflare forwards the requests through that connection. No port of your
host has to be open, and Cloudflare's filters sit in front. You need a domain
whose DNS is managed by Cloudflare (the free plan is enough).

1. In Cloudflare, *Zero Trust*, *Networks*, *Tunnels*: create a tunnel of
   type *Cloudflared* and copy its token. Do not add a hostname yet.
2. Add to `.env`:

   ```bash
   TUNNEL_TOKEN=<token>
   TRUSTED_PROXIES=<subnet of the compose network>
   COMPOSE_FILE=docker-compose.yml:docker-compose.tunnel.yml
   ```

   The subnet is what
   `docker network inspect jolt_default --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'`
   prints (for example `172.24.0.0/16`); the network exists since step 3.
   The tunnel container lives in it, and jolt only takes the visitor's
   address from a peer listed in `TRUSTED_PROXIES`. Without it all visitors
   would share one rate limit and one login lock. `COMPOSE_FILE` makes every
   later `docker compose` command include the tunnel file, which also hardens
   the app container (read-only file system, no capabilities, memory and
   process limits).
3. `docker compose up -d`, then check `docker logs jolt-cloudflared`: it
   should report four registered tunnel connections.
4. In the tunnel, add a public hostname: `jolt.<your domain>`, type `HTTP`,
   URL `jolt-app:8000`.
5. In Cloudflare: *SSL/TLS*, *Edge Certificates*: switch on *Always Use HTTPS*
   and set the minimum TLS version to 1.2. A rate limiting rule for the path
   `/api/login` adds a second wall to the app's own login limit. If you switch
   on *Bot Fight Mode*, test a logger in the car afterwards
   (`/api/live/report`): it may be turned away.

### Option B: your own reverse proxy

Any proxy that passes WebSockets works. With [Caddy](https://caddyserver.com)
on the same host, which fetches the certificate by itself (ports 80 and 443
must be reachable from the internet):

```
jolt.example.org {
    reverse_proxy 127.0.0.1:8322
}
```

Then set `TRUSTED_PROXIES` in `.env` to the gateway of jolt's Docker network
and run `docker compose up -d` again. Docker hands the proxy's connection to
the container from that gateway, not from 127.0.0.1, so this is the address
jolt has to trust before it takes the visitor's address from
`X-Forwarded-For`. Without it all visitors share one rate limit and one login
lock. The gateway is what
`docker network inspect jolt_default --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'`
prints (for example `172.24.0.1`). With nginx, forward the `Upgrade` and `Connection` headers for
`/api/live/`.

## 5. First steps in the app

Open `https://jolt.<your domain>` and log in with `APP_PASSWORT`. Logs, if
something does not work: `docker compose logs -f jolt-app`. On the
phone, add the page to the home screen (Safari: *Share*, *Add to Home
Screen*); it then runs as an app and keeps working with poor reception.

Then fill the charger database, which is empty on a new installation.
Download "Ladesäulenregister" (CSV) from the
[Bundesnetzagentur](https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/E-Mobilitaet/Ladesaeulenkarte/start.html),
save it as `ladesaeulenregister.csv` next to `docker-compose.yml` and import
it:

```bash
docker compose run --rm --no-deps -v "$PWD/ladesaeulenregister.csv:/tmp/chargers.csv:ro" \
  jolt-app python tools/import_bnetza.py /tmp/chargers.csv
```

Outside Germany, import from Open Charge Map instead (needs `OCM_API_KEY`;
countries as ISO codes, then the number of chargers per country and the
minimum power in kW):

```bash
docker compose run --rm --no-deps jolt-app python tools/import_ocm.py AT,CH 5000 50
```

Both imports can be repeated; they update instead of duplicating. How to use
the app itself is in the [README](../README.md#using-jolt).

## 6. Notifications (optional)

jolt can tell the phone when the plan changes during a trip, with the screen
off:

```bash
docker compose run --rm --no-deps jolt-app python tools/push_keyname.py
```

Copy the three lines it prints into `.env`, set `VAPID_SUBJECT` to
`mailto:<your address>`, and run `docker compose up -d`. The app asks for
permission when a trip starts. On iOS this only works for the app added to
the home screen. *Einstellungen*, *Testnachricht senden* sends a test
message.

## Updating

```bash
docker compose pull jolt-app
docker compose exec jolt-app python tools/update_guard.py && docker compose up -d jolt-app
```

`update_guard.py` exits with 75 while a trip is being recorded; a restart
would cut the phone's connection in the middle of it. Wait and try again.

For automatic updates, uncomment `COMPOSE_PROFILES=autoupdate` in `.env` and
run `docker compose up -d`. Watchtower then looks for a new image every ten
minutes and uses the same guard. It needs the Docker socket, which amounts to
root on the host, so decide whether that is worth it to you.

The image tag `latest` follows `main`. To stay on a version you have checked,
use the commit tag instead (`ghcr.io/cali1205/jolt:<commit sha>`) in
`docker-compose.yml`.

## Backups

`jolt-backup` writes `data/backups/jolt-<date>.sql.gz` every night and keeps
14 days (`BACKUP_KEEP_DAYS`). Copy that directory off the host now and then.
To restore, stop the app, empty the database (the app creates its tables on
start, so a running installation is never empty) and load the dump:

```bash
docker compose stop jolt-app
docker exec jolt-db psql -U jolt -d postgres -c 'DROP DATABASE jolt' -c 'CREATE DATABASE jolt OWNER jolt'
gunzip -c data/backups/jolt-<date>.sql.gz | docker exec -i jolt-db psql -U jolt jolt
docker compose start jolt-app
```

## Removing jolt

`docker compose down` stops and removes the containers; delete `./data` (or
`JOLT_DATA`) to remove the database and backups as well.

## Security checklist

- `docker ps` shows no port of jolt bound to `0.0.0.0`. The same goes for
  anything else on the host: Docker publishes past `ufw`.
- `APP_PASSWORT` and `DB_PASSWORD` are long random values.
  `tools/rotate_secrets.sh` renews both on the host and refuses to run during
  a trip (`JOLT_DIR=$PWD DRY_RUN=1 bash tools/rotate_secrets.sh` shows what it
  would do).
- `https://jolt.<your domain>/api/docs` answers 404 (`ENABLE_API_DOCS` is
  empty).
- `curl -sI https://jolt.<your domain>/` shows the security headers
  (`content-security-policy`, `strict-transport-security`, ...).
- A wrong password from one network does not lock out a second device on
  another network; if it does, `TRUSTED_PROXIES` is missing.

## Optional extras

- **iPhone app with CarPlay:** [ios-app.md](ios-app.md). It needs your own
  Apple developer account.
- **Grafana dashboards** for recorded trips:
  [tools/grafana/README.md](../tools/grafana/README.md).
- **CI-triggered updates** for a fork: set `WATCHTOWER_HTTP_API_ENDPOINTS` and
  `WATCHTOWER_HTTP_API_TOKEN`, publish `watchtower:8080` through the tunnel
  behind a Cloudflare Access service token, and store `WATCHTOWER_URL`,
  `WATCHTOWER_TOKEN`, `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` as
  GitHub secrets. The CI step "Trigger Watchtower" then calls it after every
  image push.
