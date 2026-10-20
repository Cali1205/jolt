#!/usr/bin/env python3
"""Builds the logbook dashboard "jolt Fahrtenbuch" (uid jolt-logbook).

    python tools/grafana/build_logbook.py                 # writes logbook.json
    python tools/grafana/build_logbook.py --push URL TOKEN_FILE

Needs the views grafana_drives, grafana_charges and grafana_reports of
`setup.sql`. Unlike "jolt Fahrten" (one recording in detail) this one looks at
every drive and charge the VW data or a recording saw, over months.
"""
import json
import sys
from pathlib import Path

import build_dashboard as base
from build_analysis import UID as ANALYSIS_UID, bars
from build_dashboard import _panel, sql

UID = "jolt-logbook"
OUT = Path(__file__).with_name("logbook.json")

STAT_OPTIONS = {"colorMode": "none", "graphMode": "none", "textMode": "value_and_name",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}

# Consumption from the charge level, weighted by distance: the mean of the
# per-drive values would give a 15 km drive the same say as a 300 km one.
SOC_KWH100 = "sum(kwh100_soc * km) / nullif(sum(km) FILTER (WHERE kwh100_soc IS NOT NULL), 0)"
DONGLE_KWH100 = ("100 * sum(kwh_dongle) / nullif(sum(km) FILTER "
                 "(WHERE kwh_dongle IS NOT NULL AND km >= 1), 0)")


def time_bars(title, x, y, w, h, query, unit=None, stacked=False, description=None):
    defaults = {"custom": {"drawStyle": "bars", "fillOpacity": 70, "lineWidth": 1,
                           "barAlignment": -1,
                           "stacking": {"mode": "normal" if stacked else "none"}}}
    if unit:
        defaults["unit"] = unit
    return _panel("timeseries", title, x, y, w, h, [sql(query, "time_series")],
                  options={"legend": {"displayMode": "list", "placement": "bottom"},
                           "tooltip": {"mode": "multi", "sort": "none"}},
                  field_config={"defaults": defaults, "overrides": []},
                  description=description)


def lines(title, x, y, w, h, query, unit=None, right=None, description=None):
    defaults = {"custom": {"lineWidth": 2, "fillOpacity": 6, "showPoints": "auto",
                           "spanNulls": True}}
    if unit:
        defaults["unit"] = unit
    overrides = [{"matcher": {"id": "byName", "options": name},
                  "properties": [{"id": "unit", "value": axis_unit},
                                 {"id": "custom.axisPlacement", "value": "right"}]}
                 for name, axis_unit in (right or {}).items()]
    return _panel("timeseries", title, x, y, w, h, [sql(query, "time_series")],
                  options={"legend": {"displayMode": "list", "placement": "bottom"},
                           "tooltip": {"mode": "multi", "sort": "none"}},
                  field_config={"defaults": defaults, "overrides": overrides},
                  description=description)


def build() -> dict:
    panels = []
    where = "$__timeFilter(time)"

    panels.append(_panel(
        "stat", "Fahrten", 0, 0, 12, 4,
        [sql(f"""SELECT count(*) AS "Fahrten", round(sum(km)::numeric) AS "km",
       round(avg(km)::numeric, 1) AS "Ø km je Fahrt",
       round(({SOC_KWH100})::numeric, 1) AS "Ø kWh/100 km",
       count(*) FILTER (WHERE recorded) AS "aufgezeichnet"
FROM grafana_drives WHERE {where}""")],
        options=STAT_OPTIONS,
        description="Ø kWh/100 km aus dem Rückgang des Ladestands mal Akkukapazität, "
                    "nach Strecke gewichtet, nur Fahrten ab 15 km (der Ladestand hat ganze Prozent)."))
    panels.append(_panel(
        "stat", "Laden", 12, 0, 12, 4,
        [sql(f"""SELECT count(*) AS "Ladevorgänge", round(sum(energy_kwh)::numeric) AS "kWh",
       round((sum(energy_kwh) FILTER (WHERE charge_type = 'DC'))::numeric) AS "davon DC kWh",
       round(sum(cost_eur)::numeric) AS "€ (Hauspreis)",
       round(avg(kwh_per_pct)::numeric, 3) AS "kWh je %"
FROM grafana_charges WHERE {where}""")],
        options=STAT_OPTIONS,
        description="€ mit dem Strompreis aus dem Fahrzeugprofil, also ohne die Preise an "
                    "Schnellladern. kWh je % ist die Energie, die ein Prozent Ladestand kostet "
                    "(Ladevorgänge ab 5 % Gewinn) - steigende Werte heißen mehr Ladeverluste, "
                    "fallende eine kleinere nutzbare Kapazität."))

    panels.append(time_bars(
        "Kilometer pro Woche", 0, 4, 12, 8,
        f"""SELECT date_trunc('week', time) AS time, round(sum(km)::numeric) AS "km"
FROM grafana_drives WHERE {where} GROUP BY 1 ORDER BY 1""", unit="lengthkm"))
    panels.append(time_bars(
        "Geladene Energie pro Monat", 12, 4, 12, 8,
        f"""SELECT date_trunc('month', time) AS time,
       round((sum(energy_kwh) FILTER (WHERE charge_type = 'AC'))::numeric) AS "AC",
       round((sum(energy_kwh) FILTER (WHERE charge_type = 'DC'))::numeric) AS "DC"
FROM grafana_charges WHERE {where} GROUP BY 1 ORDER BY 1""", unit="kwatth", stacked=True))

    panels.append(lines(
        "Verbrauch pro Monat (kWh/100 km)", 0, 12, 12, 8,
        f"""SELECT date_trunc('month', time) AS time,
       round(({SOC_KWH100})::numeric, 1) AS "aus Ladestand",
       round(({DONGLE_KWH100})::numeric, 1) AS "Dongle"
FROM grafana_drives WHERE {where} GROUP BY 1 ORDER BY 1""",
        description="Zwei Messungen derselben Größe: der Rückgang des Ladestands (alle Fahrten "
                    "ab 15 km) und die Energiezähler des Dongles (nur aufgezeichnete Fahrten). "
                    "Der Winter zeigt sich hier zuerst."))
    panels.append(lines(
        "Kilometerstand", 12, 12, 12, 8,
        f"""SELECT ended_at AS time, odo_end AS "Kilometerstand"
FROM grafana_drives WHERE {where} AND odo_end IS NOT NULL ORDER BY 1""", unit="lengthkm"))

    panels.append(bars(
        "Fahrten nach Uhrzeit", 0, 20, 8, 8,
        f"""SELECT lpad(hour::int::text, 2, '0') AS "Klasse", count(*) AS "Fahrten",
       round(sum(km)::numeric) AS "km"
FROM grafana_drives WHERE {where} GROUP BY hour ORDER BY hour"""))
    panels[-1]["fieldConfig"]["overrides"] = [{
        "matcher": {"id": "byName", "options": "km"},
        "properties": [{"id": "custom.axisPlacement", "value": "right"}]}]
    panels.append(bars(
        "Fahrtlängen", 8, 20, 8, 8,
        f"""SELECT cls AS "Klasse", count(*) AS "Fahrten", round(sum(km)::numeric) AS "km"
FROM (SELECT km, CASE WHEN km < 5 THEN '1: <5' WHEN km < 10 THEN '2: 5-10'
                      WHEN km < 20 THEN '3: 10-20' WHEN km < 50 THEN '4: 20-50'
                      WHEN km < 150 THEN '5: 50-150' ELSE '6: 150+' END AS cls
      FROM grafana_drives WHERE {where}) q GROUP BY cls ORDER BY cls""",
        description="Wenige lange Fahrten machen oft den Großteil der Kilometer aus."))
    panels[-1]["fieldConfig"]["overrides"] = [{
        "matcher": {"id": "byName", "options": "km"},
        "properties": [{"id": "custom.axisPlacement", "value": "right"}]}]
    panels.append(bars(
        "Ladestand bei Ladebeginn", 16, 20, 8, 8,
        f"""SELECT lpad((floor(soc_start / 10) * 10)::int::text, 3, ' ') || ' %' AS "Klasse",
       count(*) FILTER (WHERE charge_type = 'AC') AS "AC",
       count(*) FILTER (WHERE charge_type = 'DC') AS "DC"
FROM grafana_charges WHERE {where} AND soc_start IS NOT NULL AND energy_kwh >= 1
GROUP BY 1 ORDER BY 1""", stacking="normal",
        description="Ladevorgänge ab 1 kWh: wie weit der Akku leer war, als geladen wurde."))

    panels.append(bars(
        "Verbrauch nach Wochentag (aus Ladestand)", 0, 28, 12, 8,
        f"""SELECT CASE weekday::int WHEN 1 THEN '1 Mo' WHEN 2 THEN '2 Di' WHEN 3 THEN '3 Mi'
                   WHEN 4 THEN '4 Do' WHEN 5 THEN '5 Fr' WHEN 6 THEN '6 Sa' ELSE '7 So' END AS "Klasse",
       round(({SOC_KWH100})::numeric, 1) AS "kWh/100 km",
       round(sum(km)::numeric) AS "km"
FROM grafana_drives WHERE {where} GROUP BY weekday ORDER BY weekday"""))
    panels[-1]["fieldConfig"]["overrides"] = [{
        "matcher": {"id": "byName", "options": "km"},
        "properties": [{"id": "custom.axisPlacement", "value": "right"}]}]
    panels.append(_panel(
        "xychart", "Ladevorgänge: Energie über Ladestand-Gewinn", 12, 28, 12, 8,
        [sql(f"""SELECT soc_gain AS "Ladestand-Gewinn %",
       CASE WHEN charge_type = 'AC' THEN energy_kwh END AS "AC kWh",
       CASE WHEN charge_type = 'DC' THEN energy_kwh END AS "DC kWh"
FROM grafana_charges WHERE {where} AND soc_gain > 0 ORDER BY 1""")],
        options={"seriesMapping": "auto", "tooltip": {"mode": "single"},
                 "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}},
        field_config={"defaults": {"custom": {"show": "points", "pointSize": {"fixed": 6}}},
                      "overrides": []},
        description="Liegen die Punkte auf einer Geraden, passt die Ladeenergie zum Ladestand. "
                    "Ihre Steigung ist die Energie je Prozent, also etwa ein Hundertstel der "
                    "nutzbaren Kapazität plus Ladeverluste."))

    drive_link = ("/d/" + base.UID + "?orgId=1&var-session=${__data.fields.Aufzeichnung}"
                  "&from=${__data.fields.von}&to=${__data.fields.bis}")
    panels.append(_panel(
        "table", "Fahrten (Aufzeichnung anklicken: Details)", 0, 36, 14, 12,
        [sql(f"""SELECT time AS "Start", round(duration_min::numeric) AS "Min",
       round(km::numeric, 1) AS "km", soc_start AS "SoC Start", soc_end AS "SoC Ende",
       round(kwh100_soc::numeric, 1) AS "kWh/100 Ladestand",
       round(kwh100_dongle::numeric, 1) AS "kWh/100 Dongle",
       round(speed_avg::numeric) AS "Ø km/h", odo_end AS "Kilometerstand",
       replace(replace(replace(sources, 'vw_history', 'VW'), 'vw_portal', 'VW'), '"', '') AS "Quelle",
       live_session_id AS "Aufzeichnung",
       (extract(epoch FROM time) * 1000)::bigint AS von,
       (extract(epoch FROM ended_at) * 1000 + 60000)::bigint AS bis
FROM grafana_drives WHERE {where} ORDER BY time DESC LIMIT 500""")],
        options={"showHeader": True, "cellHeight": "sm"},
        field_config={"defaults": {"custom": {"align": "auto"}}, "overrides": [
            {"matcher": {"id": "byName", "options": "Aufzeichnung"},
             "properties": [{"id": "links", "value": [
                 {"title": "Aufzeichnung in jolt Fahrten", "url": drive_link}]}]},
            {"matcher": {"id": "byRegexp", "options": "^(von|bis)$"},
             "properties": [{"id": "custom.hidden", "value": True}]},
            {"matcher": {"id": "byRegexp", "options": "^kWh/100"},
             "properties": [
                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                 {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                     {"color": "green", "value": None},
                     {"color": "yellow", "value": 20},
                     {"color": "red", "value": 24}]}}]}]}))
    panels.append(_panel(
        "table", "Ladevorgänge", 14, 36, 10, 12,
        [sql(f"""SELECT time AS "Start", charge_type AS "Art", round(duration_min::numeric) AS "Min",
       round(energy_kwh::numeric, 1) AS "kWh", round(max_power_kw::numeric) AS "max kW",
       soc_start AS "SoC Start", soc_end AS "SoC Ende",
       round(kwh_per_pct::numeric, 3) AS "kWh je %", odometer_km AS "Kilometerstand"
FROM grafana_charges WHERE {where} ORDER BY time DESC LIMIT 500""")],
        options={"showHeader": True, "cellHeight": "sm"},
        field_config={"defaults": {"custom": {"align": "auto"}}, "overrides": [
            {"matcher": {"id": "byName", "options": "Art"},
             "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "mappings", "value": [{"type": "value", "options": {
                                "DC": {"color": "orange", "index": 0},
                                "AC": {"color": "green", "index": 1}}}]}]}]}))

    panels.append(lines(
        "VW-Portal: Ladestand und Reichweite", 0, 48, 12, 8,
        f"""SELECT time, soc_pct AS "Ladestand %", range_km AS "Reichweite km"
FROM grafana_reports WHERE {where} ORDER BY time""",
        unit="percent", right={"Reichweite km": "lengthkm"},
        description="Jede Meldung des Autos an das VW-Portal (beim Ein- und Ausschalten, "
                    "beim Laden). Erst seit dem Einschalten des Portal-Abrufs."))
    panels.append(lines(
        "VW-Portal: Akkutemperatur und nutzbare Kapazität", 12, 48, 12, 8,
        f"""SELECT time, batt_temp_min_c AS "Akku min °C", batt_temp_max_c AS "Akku max °C",
       capacity_kwh AS "nutzbar kWh"
FROM grafana_reports WHERE {where} ORDER BY time""",
        unit="celsius", right={"nutzbar kWh": "kwatth"},
        description="Die nutzbare Kapazität (maximaler Energieinhalt laut Auto) sinkt mit dem "
                    "Alter des Akkus - über Monate betrachtet ist das die Akkugesundheit."))

    return {
        "uid": UID, "title": "jolt Fahrtenbuch", "tags": ["jolt"], "timezone": "browser",
        "schemaVersion": 41, "version": 1, "refresh": "",
        "time": {"from": "now-1y", "to": "now"},
        "templating": {"list": []},
        "panels": panels,
        "links": [{"title": "jolt Fahrten", "type": "link", "url": "/d/" + base.UID,
                   "icon": "external link"},
                  {"title": "jolt Analyse", "type": "link", "url": "/d/" + ANALYSIS_UID,
                   "icon": "external link"}],
    }


if __name__ == "__main__":
    dashboard = build()
    OUT.write_text(json.dumps(dashboard, indent=1, ensure_ascii=False), encoding="utf-8")
    if "--push" in sys.argv:
        position = sys.argv.index("--push")
        base.push(sys.argv[position + 1], sys.argv[position + 2], dashboard)
