# Grafana dashboard for the trips

Read-only view of the recordings in Grafana (tested with 13.2).

1. `setup.sql`: creates the views (`grafana_points`, `grafana_sessions`,
   `grafana_trips`, `grafana_windows`, and for the logbook `grafana_drives`,
   `grafana_charges`, `grafana_reports`) and the role `grafana_ro` (sees only
   these views, read-only). Run once:
   `docker exec -i jolt-db psql -U jolt -d jolt -v pw="'<password>'" < tools/grafana/setup.sql`
2. The database port has to be reachable from Grafana. `docker-compose.yml`
   does not publish it; add `ports: ["<LAN or VPN address>:5432:5432"]` to
   `jolt-db`, never `0.0.0.0` (Docker publishes past the host firewall).
3. In Grafana a PostgreSQL data source with the uid `jolt-pg` (user
   `grafana_ro`, database `jolt`, TLS off).
4. `python tools/grafana/build_dashboard.py --push <grafana url> <token file>`
   writes `dashboard.json`, runs every panel query once against Grafana and
   saves the dashboard `jolt-trips`. Without `--push` it only writes the JSON
   (import it in Grafana by hand). The same for the other two dashboards:
   `build_analysis.py` (`jolt-analysis`, correlations) and `build_logbook.py`
   (`jolt-logbook`, every drive and charge from the logbook over months).

After an update that adds views, run `setup.sql` again **without** `-v pw`:
it replaces the views and grants and leaves the password of `grafana_ro`
alone:
`docker exec -i jolt-db psql -U jolt -d jolt < tools/grafana/setup.sql`

Every dashboard has a "Fahrzeug" picker at the top and shows one car at a time.
The views got a `vehicle_id` column for it, so run `setup.sql` again (without
`-v pw`) after this update, before importing the dashboards.

Click a trip ID in the table: the dashboard jumps to that trip and sets the time
range to it. The panels of the selected trip use the dashboard time range, so
choosing a trip in the drop-down alone leaves the time axis where it was.
