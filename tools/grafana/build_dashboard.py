#!/usr/bin/env python3
"""Builds the jolt trip dashboard and (optionally) pushes it to Grafana.

    python tools/grafana/build_dashboard.py                 # writes dashboard.json
    python tools/grafana/build_dashboard.py --push URL TOKEN_FILE

The data comes from the views in `setup.sql` through a PostgreSQL data source
with the uid `jolt-pg`. With --push every panel query is also run once against
Grafana (latest session filled in), so a typo shows up here and not as an empty
panel.
"""
import json
import sys
import urllib.request
from pathlib import Path

DS = {"type": "grafana-postgresql-datasource", "uid": "jolt-pg"}
UID = "jolt-trips"
OUT = Path(__file__).with_name("dashboard.json")

_next_id = [0]
COLOUR_CHOICES = ("Tempo : speed_kmh, Leistung : power_kw, Ladestand : soc, "
                  "Abweichung vom Plan : (soc - plan_soc), Nebenverbraucher : aux_load_kw, "
                  "Drehmoment : torque_rear_nm, Höhe : elevation_m, "
                  "Akkutemperatur : batt_temp_max_c, Zellspannung min : cell_min_v, "
                  "Außentemperatur : outside_temp_c")
AXIS_LABELS = {"kwatt": "kW", "watt": "W", "celsius": "°C", "velocitykmh": "km/h",
               "lengthm": "m", "volt": "V", "percent": "%"}


def _panel(kind, title, x, y, w, h, targets, options=None, field_config=None,
           description=None):
    _next_id[0] += 1
    panel = {
        "id": _next_id[0], "type": kind, "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DS,
        "targets": [dict(t, datasource=DS, refId=chr(65 + i), editorMode="code")
                    for i, t in enumerate(targets)],
        "options": options or {},
        "fieldConfig": field_config or {"defaults": {}, "overrides": []},
    }
    if description:
        panel["description"] = description
    return panel


def sql(query, fmt="table"):
    return {"rawSql": query, "format": fmt}


def series(title, x, y, w, h, columns, axes=None, unit=None, description=None,
           where="TRUE"):
    """Time series of columns of grafana_points for the chosen session.

    `axes` maps a column to (unit, side) for a second axis.
    """
    select = ", ".join(columns)
    query = (f"SELECT time, {select} FROM grafana_points "
             f"WHERE session_id = ${{session}} AND {where} ORDER BY time")
    overrides = []
    for column, (axis_unit, side) in (axes or {}).items():
        overrides.append({
            "matcher": {"id": "byName",
                        "options": column.split(" AS ")[-1].strip('"')},
            "properties": [
                {"id": "unit", "value": axis_unit},
                {"id": "custom.axisPlacement", "value": side},
                {"id": "custom.axisLabel", "value": AXIS_LABELS.get(axis_unit, "")},
            ]})
    defaults = {"custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never",
                           "spanNulls": 600000}}
    if unit:
        defaults["unit"] = unit
        defaults["custom"]["axisLabel"] = AXIS_LABELS.get(unit, "")
    return _panel(
        "timeseries", title, x, y, w, h, [sql(query, "time_series")],
        options={"legend": {"displayMode": "list", "placement": "bottom"},
                 "tooltip": {"mode": "multi", "sort": "none"}},
        field_config={"defaults": defaults, "overrides": overrides},
        description=description)


def build() -> dict:
    panels = []

    # --- All trips -------------------------------------------------------
    link = ("/d/" + UID + "?orgId=1&var-session=${__data.fields.id}"
            "&from=${__data.fields.von}&to=${__data.fields.bis}")
    panels.append(_panel(
        "table", "Fahrten (ID anklicken: Fahrt unten anzeigen)", 0, 0, 14, 10,
        [sql("""
SELECT id, time AS "Start", round((extract(epoch FROM duration) / 60)::numeric) AS "Min",
       round(km_odometer::numeric) AS "km", round(net_kwh::numeric, 2) AS "kWh netto",
       round(kwh_per_100km::numeric, 1) AS "kWh/100 km",
       round(soc_start::numeric, 1) AS "SoC Start", round(soc_end::numeric, 1) AS "SoC Ende",
       round(outside_c::numeric, 1) AS "Außen °C", round(speed_max_kmh::numeric) AS "vmax",
       round(aux_avg_kw::numeric, 2) AS "Neben kW", points AS "Punkte",
       (extract(epoch FROM time) * 1000)::bigint AS von,
       (extract(epoch FROM coalesce(last_point, time)) * 1000 + 60000)::bigint AS bis
FROM grafana_sessions ORDER BY id DESC LIMIT 200""")],
        options={"showHeader": True, "cellHeight": "sm"},
        field_config={
            "defaults": {"custom": {"align": "auto"}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "id"},
                 "properties": [{"id": "links", "value": [
                     {"title": "Fahrt anzeigen", "url": link}]}]},
                {"matcher": {"id": "byRegexp", "options": "^(von|bis)$"},
                 "properties": [{"id": "custom.hidden", "value": True}]},
                {"matcher": {"id": "byName", "options": "kWh/100 km"},
                 "properties": [
                     {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                     {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                         {"color": "green", "value": None},
                         {"color": "yellow", "value": 20},
                         {"color": "red", "value": 24}]}}]},
            ]}))

    panels.append(_panel(
        "stat", "Alle Fahrten", 14, 0, 10, 4,
        [sql("""
SELECT count(*) AS "Fahrten", round(sum(km_odometer)::numeric) AS "km",
       round((100 * sum(net_kwh) FILTER (WHERE km_odometer >= 3)
              / nullif(sum(km_odometer) FILTER (WHERE km_odometer >= 3), 0))::numeric, 1)
         AS "Ø kWh/100 km"
FROM grafana_sessions""")],
        options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name",
                 "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        description="Ø nur über Fahrten ab 3 km (der Kilometerzähler ist ganzzahlig)."))

    panels.append(_panel(
        "barchart", "Verbrauch je Fahrt (letzte 40, ab 3 km) und Außentemperatur", 14, 4, 10, 6,
        [sql("""
SELECT '#' || id AS "Fahrt", round(kwh_per_100km::numeric, 1) AS "kWh/100 km",
       round(outside_c::numeric, 1) AS "Außen °C"
FROM (SELECT * FROM grafana_sessions WHERE kwh_per_100km IS NOT NULL
      ORDER BY id DESC LIMIT 40) t ORDER BY id""", "table")],
        options={"xField": "Fahrt", "orientation": "vertical", "showValue": "never",
                 "legend": {"displayMode": "list", "placement": "bottom"},
                 "xTickLabelRotation": -45, "tooltip": {"mode": "multi"}},
        field_config={"defaults": {"custom": {"fillOpacity": 70}}, "overrides": [
            {"matcher": {"id": "byName", "options": "Außen °C"},
             "properties": [{"id": "custom.axisPlacement", "value": "right"},
                            {"id": "unit", "value": "celsius"}]}]}))

    # --- One trip --------------------------------------------------------
    panels.append(_panel(
        "stat", "Fahrt ${session}", 0, 10, 24, 4,
        [sql("""
SELECT round(km_odometer::numeric) AS "km", round(kwh_per_100km::numeric, 1) AS "kWh/100 km",
       round(net_kwh::numeric, 2) AS "kWh netto", round((soc_start - soc_end)::numeric, 1) AS "SoC verbraucht",
       round(aux_avg_kw::numeric, 2) AS "Ø Neben kW", round(speed_max_kmh::numeric) AS "vmax km/h",
       round(outside_c::numeric, 1) AS "Außen °C",
       round((extract(epoch FROM duration) / 60)::numeric) AS "Dauer min"
FROM grafana_sessions WHERE id = ${session}""")],
        options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name",
                 "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}))

    panels.append(_panel(
        "geomap", "Route (Farbe = ${colour:text})", 0, 14, 12, 12,
        [sql("SELECT lat, lon, ${colour} AS value, time FROM grafana_points "
             "WHERE session_id = ${session} AND lat IS NOT NULL "
             "AND ${colour} IS NOT NULL ORDER BY time")],
        options={
            "view": {"id": "fit", "allLayers": True, "zoom": 12},
            "controls": {"showZoom": True, "mouseWheelZoom": True},
            "basemap": {"type": "osm-standard", "name": "Basemap"},
            "layers": [{"type": "markers", "name": "Route", "location": {
                "mode": "coords", "latitude": "lat", "longitude": "lon"},
                "config": {"showLegend": True, "style": {
                    "size": {"fixed": 4, "min": 2, "max": 6},
                    "color": {"field": "value", "fixed": "dark-green"},
                    "opacity": 0.9,
                    "symbol": {"mode": "fixed", "fixed": "img/icons/marker/circle.svg"}}}}]},
        field_config={"defaults": {"color": {"mode": "continuous-GrYlRd"}},
                      "overrides": []},
        description="Der Wert für die Farbe der Punkte ist oben über „Farbe“ wählbar; "
                    "die Skala läuft vom kleinsten zum größten Wert der Fahrt "
                    "(grün = klein, rot = groß)."))

    panels.append(series(
        "Tempo und Leistung", 12, 14, 12, 6,
        ["speed_kmh AS \"Tempo km/h\"", "power_kw AS \"Leistung kW\""],
        unit="velocitykmh", axes={"power_kw AS \"Leistung kW\"": ("kwatt", "right")},
        description="Leistung: Entladung positiv, Rekuperation negativ."))

    panels.append(series(
        "Ladestand gegen Plan", 12, 20, 12, 6,
        ["soc AS \"Ladestand %\"", "plan_soc AS \"Plan %\""], unit="percent",
        description="Liegt der Ladestand unter der Plan-Linie, verbraucht die Fahrt mehr als geplant."))

    panels.append(_panel(
        "timeseries", "Verbrauch (30-s-Mittel)", 0, 26, 12, 6,
        [sql("""
SELECT date_bin('30 seconds', time, TIMESTAMP '2000-01-01') AS time,
       round((avg(power_kw) / avg(speed_kmh) * 100)::numeric, 1) AS "kWh/100 km"
FROM grafana_points
WHERE session_id = ${session} AND speed_kmh IS NOT NULL AND power_kw IS NOT NULL
GROUP BY 1 HAVING avg(speed_kmh) > 15 ORDER BY 1""", "time_series")],
        options={"legend": {"displayMode": "list", "placement": "bottom"},
                 "tooltip": {"mode": "multi", "sort": "none"}},
        field_config={"defaults": {"custom": {"lineWidth": 2, "fillOpacity": 8,
                                              "showPoints": "auto", "spanNulls": 600000},
                                   "unit": "none"}, "overrides": []},
        description="Mittlere Leistung durch mittleres Tempo je 30 s, nur über 15 km/h. Negative Werte sind Rekuperation."))

    panels.append(series(
        "Nebenverbraucher", 12, 26, 12, 6,
        ["aux_load_kw AS \"Neben kW\"", "compressor_w AS \"Kompressor W\""],
        unit="kwatt", axes={"compressor_w AS \"Kompressor W\"": ("watt", "right")}))

    panels.append(series(
        "Höhe und Tempo", 0, 32, 12, 6,
        ["elevation_m AS \"Höhe m\"", "speed_kmh AS \"Tempo km/h\""],
        unit="lengthm", axes={"speed_kmh AS \"Tempo km/h\"": ("velocitykmh", "right")}))

    panels.append(series(
        "Zellspannung und Akkutemperatur", 12, 32, 12, 6,
        ["cell_min_v AS \"Zelle min V\"", "cell_max_v AS \"Zelle max V\"",
         "batt_temp_min_c AS \"Akku min °C\"", "batt_temp_max_c AS \"Akku max °C\""],
        unit="volt", axes={"batt_temp_min_c AS \"Akku min °C\"": ("celsius", "right"),
              "batt_temp_max_c AS \"Akku max °C\"": ("celsius", "right")},
        description="Zeilen mit Messwerten gibt es nur in jedem 2.-6. Punkt (rotierende Abfrage)."))

    return {
        "uid": UID, "title": "jolt Fahrten", "tags": ["jolt"], "timezone": "browser",
        "schemaVersion": 41, "version": 1, "refresh": "",
        "time": {"from": "now-7d", "to": "now"},
        "templating": {"list": [{
            "name": "session", "label": "Fahrt", "type": "query", "datasource": DS,
            "query": ("SELECT id AS __value, '#' || id || '  ' || to_char(time, 'DD.MM. HH24:MI')"
                      " || '  ' || coalesce(round(km_odometer)::text, '?') || ' km' AS __text "
                      "FROM grafana_sessions ORDER BY id DESC"),
            "definition": "grafana_sessions", "refresh": 2, "sort": 0,
            "includeAll": False, "multi": False}, {
            "name": "colour", "label": "Farbe", "type": "custom",
            "query": COLOUR_CHOICES, "refresh": 0,
            "current": {"text": "Tempo", "value": "speed_kmh"},
            "includeAll": False, "multi": False}]},
        "panels": panels,
        "links": [],
    }


def push(url: str, token_file: str, dash: dict) -> None:
    raw = Path(token_file).read_bytes()
    text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8", "ignore")
    token = "".join(c for c in text if c.isalnum() or c in "_=-")

    def call(path, body=None):
        request = urllib.request.Request(
            url + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as reply:
            return json.load(reply)

    latest = call("/api/ds/query", {"queries": [{
        "refId": "A", "datasource": DS, "format": "table",
        "rawSql": "SELECT max(id) FROM grafana_sessions"}], "from": "now-1h", "to": "now"})
    session = latest["results"]["A"]["frames"][0]["data"]["values"][0][0]

    problems = 0
    for panel in dash["panels"]:
        for target in panel["targets"]:
            query = target["rawSql"].replace("${session}", str(session)).replace(
                "${colour}", "speed_kmh").replace(
                "$__timeFilter(time)", "time > now() - interval '30 days'")
            result = call("/api/ds/query", {"queries": [{
                "refId": "A", "datasource": DS, "format": target["format"],
                "rawSql": query}], "from": "now-7d", "to": "now"})["results"]["A"]
            frames = result.get("frames") or []
            rows = len(frames[0]["data"]["values"][0]) if frames and frames[0]["data"]["values"] else 0
            error = result.get("error")
            problems += bool(error) or rows == 0
            print(f"{'FEHLER' if error else ('LEER ' if rows == 0 else 'ok    ')} "
                  f"{panel['title'][:46]:46} {rows:4} Zeilen {error or ''}")

    reply = call("/api/dashboards/db", {"dashboard": dash, "overwrite": True,
                                        "message": "build_dashboard.py"})
    print(f"\nDashboard gespeichert: {url}{reply['url']}  ({problems} Panel(s) mit Problem)")


if __name__ == "__main__":
    dashboard = build()
    OUT.write_text(json.dumps(dashboard, indent=1, ensure_ascii=False), encoding="utf-8")
    if "--push" in sys.argv:
        position = sys.argv.index("--push")
        push(sys.argv[position + 1], sys.argv[position + 2], dashboard)
