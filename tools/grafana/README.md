# Grafana dashboard for the trips

Read-only view of the recordings in Grafana (tested with 13.2).

1. `setup.sql`: creates the views `grafana_points` / `grafana_sessions` and the
   role `grafana_ro` (sees only these views, read-only). Run once:
   `docker exec -i jolt-db psql -U jolt -d jolt -v pw="'<password>'" < tools/grafana/setup.sql`
2. The database port has to be reachable from Grafana. In the production
   compose file `jolt-db` has `ports: ["<LAN address>:5432:5432"]` (LAN only).
3. In Grafana a PostgreSQL data source with the uid `jolt-pg` (user
   `grafana_ro`, database `jolt`, TLS off).
4. `python tools/grafana/build_dashboard.py --push <grafana url> <token file>`
   writes `dashboard.json`, runs every panel query once against Grafana and
   saves the dashboard `jolt-trips`. Without `--push` it only writes the JSON
   (import it in Grafana by hand).

Click a trip ID in the table: the dashboard jumps to that trip and sets the time
range to it. The panels of the selected trip use the dashboard time range, so
choosing a trip in the drop-down alone leaves the time axis where it was.
