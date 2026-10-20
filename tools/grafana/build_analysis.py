#!/usr/bin/env python3
"""Builds the correlation dashboard "jolt Analyse" (uid jolt-analysis).

    python tools/grafana/build_analysis.py                 # writes analysis.json
    python tools/grafana/build_analysis.py --push URL TOKEN_FILE

Needs the views of `setup.sql` (grafana_trips, grafana_windows). Two levels:
one dot per trip, and one dot per 60-second window over all trips. For both
the two axes are chosen from a list; below that are fixed standard views of the
consumption under different conditions.
"""
import json
import sys
from pathlib import Path

import build_dashboard as base
from build_dashboard import DS, _panel, sql

UID = "jolt-analysis"
OUT = Path(__file__).with_name("analysis.json")

TRIP_KPIS = [
    ("Verbrauch", "kwh100"), ("Verbrauch ohne Nebenverbraucher", "drive_kwh100"),
    ("Nebenverbrauch je 100 km", "aux_kwh100"), ("Außentemperatur", "outside_c"),
    ("Akkutemperatur", "batt_temp_c"), ("Ø Tempo", "speed_avg"),
    ("Höchsttempo", "speed_max"), ("Ø Nebenverbraucher kW", "aux_kw"),
    ("Anstieg m je km", "ascent_per_km"), ("Stillstandsanteil %", "stop_share_pct"),
    ("Rekuperation %", "regen_pct"), ("Strecke km", "km"),
    ("Dauer min", "duration_min"), ("Ladestand Start", "soc_start"),
]
WINDOW_KPIS = [
    ("Verbrauch", "kwh100"), ("Antriebsverbrauch", "drive_kwh100"),
    ("Nebenverbrauch je 100 km", "aux_kwh100"), ("Tempo", "speed_kmh"),
    ("Leistung kW", "power_kw"), ("Nebenverbraucher kW", "aux_kw"),
    ("Steigung %", "slope_pct"), ("Außentemperatur", "outside_c"),
    ("Akkutemperatur", "batt_temp_c"),
]


def choices(pairs):
    return ", ".join(f"{label} : {column}" for label, column in pairs)


def variable(name, label, pairs, default):
    column = dict((c, l) for l, c in pairs)[default]
    return {"name": name, "label": label, "type": "custom", "query": choices(pairs),
            "refresh": 0, "includeAll": False, "multi": False,
            "current": {"text": column, "value": default}}


def scatter(title, x, y, w, h, table, xv, yv, where):
    """Points plus a fitted straight line (second query)."""
    data = (f"SELECT ${{{xv}}} AS x, ${{{yv}}} AS y FROM {table} "
            f"WHERE {where} AND ${{{xv}}} IS NOT NULL AND ${{{yv}}} IS NOT NULL")
    xl, yl = f'"${{{xv}:text}}"', f'"${{{yv}:text}}"'
    query = (f"WITH d AS ({data}), r AS (SELECT regr_slope(y, x) AS b, "
             f"regr_intercept(y, x) AS a, min(x) AS x0, max(x) AS x1 FROM d) "
             f"SELECT x AS {xl}, y AS {yl}, NULL::float AS \"Trend\" FROM d "
             f"UNION ALL SELECT x0, NULL, a + b * x0 FROM r WHERE b IS NOT NULL "
             f"UNION ALL SELECT x1, NULL, a + b * x1 FROM r WHERE b IS NOT NULL ORDER BY 1")
    return _panel(
        "xychart", title, x, y, w, h, [sql(query)],
        options={"seriesMapping": "auto", "legend": {"displayMode": "list", "placement": "bottom",
                                                      "showLegend": True},
                 "tooltip": {"mode": "single"}},
        field_config={"defaults": {"custom": {"show": "points", "pointSize": {"fixed": 7},
                                              "lineWidth": 2}},
                      "overrides": [{"matcher": {"id": "byName", "options": "Trend"},
                                     "properties": [{"id": "custom.show", "value": "lines"},
                                                    {"id": "color", "value": {
                                                        "mode": "fixed", "fixedColor": "orange"}}]}]},
        description="Jeder Punkt ist eine Auswertungseinheit; die orange Linie ist die lineare "
                    "Ausgleichsgerade. Achsen oben über die Auswahl wählen.")


def relation(title, x, y, w, h, table, xv, yv, where):
    query = (f"SELECT count(*) AS \"Punkte\", round(corr(y, x)::numeric, 2) AS \"r\", "
             f"round(regr_r2(y, x)::numeric, 2) AS \"R²\", "
             f"round(regr_slope(y, x)::numeric, 3) AS \"Steigung\" FROM "
             f"(SELECT ${{{xv}}} AS x, ${{{yv}}} AS y FROM {table} WHERE {where} "
             f"AND ${{{xv}}} IS NOT NULL AND ${{{yv}}} IS NOT NULL) t")
    return _panel(
        "stat", title, x, y, w, h, [sql(query)],
        options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name",
                 "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        description="r = Korrelation (−1 bis 1), R² = Anteil der Streuung, den die Gerade erklärt, "
                    "Steigung = Änderung von Y je Einheit X. Bei wenigen Punkten sind alle drei "
                    "mit Vorsicht zu lesen; ein Zusammenhang ist keine Ursache.")


def classes(sql_case, label):
    return f"{sql_case} AS {label}"


SPEED = ("CASE WHEN speed_kmh < 30 THEN '0-30' WHEN speed_kmh < 50 THEN '30-50' "
         "WHEN speed_kmh < 70 THEN '50-70' WHEN speed_kmh < 90 THEN '70-90' ELSE '90+' END")
TEMP = ("CASE WHEN outside_c < 10 THEN '<10 °C' WHEN outside_c < 15 THEN '10-15 °C' "
        "WHEN outside_c < 20 THEN '15-20 °C' ELSE '≥20 °C' END")
SLOPE = ("CASE WHEN slope_pct < -4 THEN '1: <-4' WHEN slope_pct < -2 THEN '2: -4..-2' "
         "WHEN slope_pct < -0.5 THEN '3: -2..-0.5' WHEN slope_pct <= 0.5 THEN '4: flach' "
         "WHEN slope_pct <= 2 THEN '5: 0.5..2' WHEN slope_pct <= 4 THEN '6: 2..4' ELSE '7: >4' END")


def bars(title, x, y, w, h, query, stacking=None, unit=None, description=None):
    options = {"xField": "Klasse", "orientation": "vertical", "showValue": "never",
               "legend": {"displayMode": "list", "placement": "bottom"},
               "tooltip": {"mode": "multi"}, "barWidth": 0.8, "groupWidth": 0.7}
    if stacking:
        options["stacking"] = stacking
    defaults = {"custom": {"fillOpacity": 80}}
    if unit:
        defaults["unit"] = unit
    return _panel("barchart", title, x, y, w, h, [sql(query)], options=options,
                  field_config={"defaults": defaults, "overrides": []}, description=description)

# The car's DC curve from the logbook and the energy one percent takes; shared
# by the bar chart and the table of the charging-time analysis.
CHARGE_TIME = """WITH env AS (
    SELECT floor(soc)::int AS pct, percentile_cont(0.9) WITHIN GROUP (ORDER BY kw) AS kw
    FROM grafana_charge_curve
    WHERE charge_type = 'DC' AND kw > 5 AND $__timeFilter(time) GROUP BY 1),
per_pct AS (
    SELECT coalesce(
        (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY kwh_per_pct)
         FROM grafana_charges WHERE charge_type = 'DC' AND soc_gain >= 20),
        (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY kwh_per_pct)
         FROM grafana_charges WHERE kwh_per_pct IS NOT NULL)) AS kwh),
per_cap AS (
    SELECT c.cap,
           round(sum(p.kwh / least(e.kw, c.cap) * 60)::numeric, 1) AS minutes,
           round((sum(p.kwh) / sum(p.kwh / least(e.kw, c.cap)))::numeric) AS avg_kw,
           round(100.0 * count(*) FILTER (WHERE e.kw > c.cap) / count(*)) AS limited,
           round(100.0 * count(*) / 70) AS covered
    FROM generate_series(185, 80, -5) AS c(cap)
    CROSS JOIN per_pct p
    JOIN env e ON e.pct BETWEEN 10 AND 79
    GROUP BY c.cap)"""


def build() -> dict:
    trips_where = "$__timeFilter(time) AND km >= ${minkm}"
    windows_where = "$__timeFilter(time) AND (slope_pct IS NULL OR abs(slope_pct) <= 15)"
    panels = []

    panels.append(_panel(
        "text", "", 0, 0, 24, 2, [], options={"mode": "markdown", "content": (
            "**Fahrten:** je Punkt eine Fahrt · **Fenster:** je Punkt 60 Sekunden Fahrt aus "
            "allen Fahrten des Zeitbereichs. Achsen und Mindeststrecke stehen oben in der Leiste.")}))
    panels[-1].pop("datasource")

    panels.append(scatter("Fahrten: ${ty:text} über ${tx:text}", 0, 2, 16, 12,
                          "grafana_trips", "tx", "ty", trips_where))
    panels.append(relation("Zusammenhang (Fahrten)", 16, 2, 8, 4, "grafana_trips", "tx", "ty",
                           trips_where))
    panels.append(_panel(
        "table", "Fahrten (Auswahl)", 16, 6, 8, 8,
        [sql('SELECT id, time AS "Start", round(km::numeric) AS "km", '
             'round((${tx})::numeric, 2) AS "${tx:text}", round((${ty})::numeric, 2) AS "${ty:text}" '
             f"FROM grafana_trips WHERE {trips_where} ORDER BY time DESC LIMIT 200")],
        options={"showHeader": True, "cellHeight": "sm"}))

    panels.append(scatter("Fenster: ${wy:text} über ${wx:text}", 0, 14, 16, 12,
                          "grafana_windows", "wx", "wy", windows_where))
    panels.append(relation("Zusammenhang (Fenster)", 16, 14, 8, 4, "grafana_windows", "wx", "wy",
                           windows_where))
    panels.append(_panel(
        "stat", "Datenbasis", 16, 18, 8, 4,
        [sql("SELECT count(*) AS \"Fenster\", count(DISTINCT session_id) AS \"Fahrten\", "
             "round(min(outside_c)::numeric, 1) AS \"Außen min °C\", "
             "round(max(outside_c)::numeric, 1) AS \"Außen max °C\" "
             f"FROM grafana_windows WHERE {windows_where}")],
        options={"colorMode": "none", "graphMode": "none", "textMode": "value_and_name",
                 "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        description="Die Außentemperatur ist je Fahrt gemittelt: der Dongle meldet sie nur alle paar Minuten."))

    # Fixed standard views.
    row = 26
    panels.append(bars(
        "Verbrauch nach Tempo und Außentemperatur (kWh/100 km)", 0, row, 12, 10,
        f"""SELECT cls AS "Klasse",
       round(avg(kwh100) FILTER (WHERE t = '<10 °C')::numeric, 1) AS "<10 °C",
       round(avg(kwh100) FILTER (WHERE t = '10-15 °C')::numeric, 1) AS "10-15 °C",
       round(avg(kwh100) FILTER (WHERE t = '15-20 °C')::numeric, 1) AS "15-20 °C",
       round(avg(kwh100) FILTER (WHERE t = '≥20 °C')::numeric, 1) AS "≥20 °C"
FROM (SELECT {SPEED} AS cls, {TEMP} AS t, kwh100, speed_kmh FROM grafana_windows
      WHERE $__timeFilter(time) AND kwh100 IS NOT NULL AND outside_c IS NOT NULL) q
GROUP BY cls ORDER BY min(speed_kmh)""",
        description="Mittlerer Verbrauch je Tempoklasse (km/h), getrennt nach Außentemperatur. "
                    "Fehlen Balken, gibt es in der Klasse keine Fenster."))
    panels.append(bars(
        "Antrieb und Nebenverbraucher nach Tempo (kWh/100 km)", 12, row, 12, 10,
        f"""SELECT cls AS "Klasse", round(avg(drive_kwh100)::numeric, 1) AS "Antrieb",
       round(avg(aux_kwh100)::numeric, 1) AS "Nebenverbraucher"
FROM (SELECT {SPEED} AS cls, drive_kwh100, aux_kwh100, speed_kmh FROM grafana_windows
      WHERE $__timeFilter(time) AND kwh100 IS NOT NULL) q
GROUP BY cls ORDER BY min(speed_kmh)""", stacking="normal",
        description="Je langsamer die Fahrt, desto größer der Anteil der Nebenverbraucher am Verbrauch."))
    panels.append(bars(
        "Nebenverbraucher nach Außentemperatur (kW)", 0, row + 10, 12, 8,
        f"""SELECT cls AS "Klasse", round(avg(aux_kw)::numeric, 2) AS "Ø Nebenverbraucher",
       round(max(aux_kw)::numeric, 2) AS "Maximum"
FROM (SELECT {TEMP} AS cls, aux_kw, outside_c FROM grafana_windows
      WHERE $__timeFilter(time) AND aux_kw IS NOT NULL AND outside_c IS NOT NULL) q
GROUP BY cls ORDER BY min(outside_c)"""))
    panels.append(bars(
        "Verbrauch nach Steigung (kWh/100 km, ab 30 km/h)", 12, row + 10, 12, 8,
        f"""SELECT cls AS "Klasse", round(avg(kwh100)::numeric, 1) AS "Verbrauch",
       count(*) AS "Fenster"
FROM (SELECT {SLOPE} AS cls, kwh100 FROM grafana_windows
      WHERE $__timeFilter(time) AND speed_kmh >= 30 AND slope_pct IS NOT NULL
        AND abs(slope_pct) <= 15 AND kwh100 IS NOT NULL) q
GROUP BY cls ORDER BY cls""",
        description="Steigung aus dem Höhenunterschied im Fenster (Höhe in ganzen Metern: grob). "
                    "Bergab sinkt der Verbrauch durch Rekuperation, negative Werte sind möglich."))
    for panel in panels[-1:]:
        panel["fieldConfig"]["overrides"] = [{
            "matcher": {"id": "byName", "options": "Fenster"},
            "properties": [{"id": "custom.axisPlacement", "value": "right"}]}]

    # The logbook: every drive, also the ones driven without the app, with
    # consumption from the drop of the charge level. Coarser than the dongle
    # (whole percent), so only drives from 15 km, but far more of them.
    row += 18
    logbook_where = "$__timeFilter(time) AND kwh100_soc IS NOT NULL"
    panels.append(_panel(
        "xychart", "Fahrtenbuch: Verbrauch über Ø Tempo (alle Fahrten ab 15 km)",
        0, row, 12, 10,
        [sql(f"""WITH d AS (SELECT speed_avg AS x, kwh100_soc AS y FROM grafana_drives
           WHERE {logbook_where} AND speed_avg BETWEEN 5 AND 160),
     r AS (SELECT regr_slope(y, x) AS b, regr_intercept(y, x) AS a,
                  min(x) AS x0, max(x) AS x1 FROM d)
SELECT x AS "Ø km/h", y AS "kWh/100 km", NULL::float AS "Trend" FROM d
UNION ALL SELECT x0, NULL, a + b * x0 FROM r WHERE b IS NOT NULL
UNION ALL SELECT x1, NULL, a + b * x1 FROM r WHERE b IS NOT NULL ORDER BY 1""")],
        options={"seriesMapping": "auto", "tooltip": {"mode": "single"},
                 "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}},
        field_config={"defaults": {"custom": {"show": "points", "pointSize": {"fixed": 5},
                                              "lineWidth": 2}},
                      "overrides": [{"matcher": {"id": "byName", "options": "Trend"},
                                     "properties": [{"id": "custom.show", "value": "lines"},
                                                    {"id": "color", "value": {
                                                        "mode": "fixed", "fixedColor": "orange"}}]}]},
        description="Ø Tempo = Strecke durch Zeit vom Einschalten bis zum Abstellen, Ampeln "
                    "und Staus eingeschlossen. Verbrauch aus dem Ladestand mal Akkukapazität."))
    panels.append(bars(
        "Fahrtenbuch: Verbrauch nach Fahrtlänge und Jahreszeit (kWh/100 km)", 12, row, 12, 10,
        f"""SELECT cls AS "Klasse",
       round((sum(kwh100_soc * km) FILTER (WHERE winter) / nullif(sum(km) FILTER (WHERE winter), 0))::numeric, 1) AS "Okt-Mär",
       round((sum(kwh100_soc * km) FILTER (WHERE NOT winter) / nullif(sum(km) FILTER (WHERE NOT winter), 0))::numeric, 1) AS "Apr-Sep"
FROM (SELECT km, kwh100_soc, extract(month FROM time) NOT BETWEEN 4 AND 9 AS winter,
             CASE WHEN km < 30 THEN '1: 15-30 km' WHEN km < 60 THEN '2: 30-60 km'
                  WHEN km < 150 THEN '3: 60-150 km' ELSE '4: 150+ km' END AS cls
      FROM grafana_drives WHERE {logbook_where}) q
GROUP BY cls ORDER BY cls""",
        description="Nach Strecke gewichtet. Kurze Fahrten im Winter kosten am meisten: "
                    "Heizen und ein kalter Akku fallen auf wenige Kilometer."))

    # What a weaker charger costs: the car's own curve (upper edge of every DC
    # charge, 90th percentile per percent of charge level) cut at the
    # charger's power, then the minutes per percent summed from 10 to 80 %.
    row += 10
    panels.append(bars(
        "Ladezeit 10–80 % je Säulenleistung (Minuten)", 0, row, 12, 10,
        f"""{CHARGE_TIME}
SELECT cap::text || ' kW' AS "Klasse", minutes AS "Minuten 10-80 %"
FROM per_cap ORDER BY cap DESC""",
        description="Bester Fall mit warmem Akku: die Ladekurve ist die obere Kante aller "
                    "DC-Ladevorgänge im Zeitraum, je Prozent Ladestand gekappt bei der "
                    "Säulenleistung. Echte Ladevorgänge dauern etwa 10-15 % länger "
                    "(Hochfahren der Leistung), mit kaltem Akku deutlich mehr."))
    panels.append(_panel(
        "table", "Ladezeit 10–80 %: was eine schwächere Säule kostet", 12, row, 12, 10,
        [sql(f"""{CHARGE_TIME}
SELECT cap AS "Säule kW", minutes AS "Min 10-80 %",
       round(minutes - min(minutes) OVER (), 1) AS "+ Min",
       round(100 * (minutes / min(minutes) OVER () - 1)) AS "+ %",
       avg_kw AS "Ø kW", limited AS "% gedrosselt", covered AS "% mit Messdaten"
FROM per_cap ORDER BY cap DESC""")],
        options={"showHeader": True, "cellHeight": "sm"},
        description="% gedrosselt: Anteil der Prozentpunkte, bei denen die Säule weniger "
                    "liefert als der Akku annehmen würde. % mit Messdaten: wie viele der 70 "
                    "Prozentpunkte zwischen 10 und 80 % durch DC-Ladevorgänge belegt sind - "
                    "fehlen welche, ist die Zeit zu kurz."))

    return {
        "uid": UID, "title": "jolt Analyse", "tags": ["jolt"], "timezone": "browser",
        "schemaVersion": 41, "version": 1, "refresh": "",
        "time": {"from": "now-90d", "to": "now"},
        "templating": {"list": [
            variable("tx", "Fahrt X", TRIP_KPIS, "outside_c"),
            variable("ty", "Fahrt Y", TRIP_KPIS, "kwh100"),
            variable("wx", "Fenster X", WINDOW_KPIS, "speed_kmh"),
            variable("wy", "Fenster Y", WINDOW_KPIS, "kwh100"),
            {"name": "minkm", "label": "Mindeststrecke km", "type": "custom",
             "query": "0,1,3,5,10,20", "refresh": 0, "includeAll": False, "multi": False,
             "current": {"text": "3", "value": "3"}}]},
        "panels": panels,
        "links": [{"title": "jolt Fahrten", "type": "link", "url": "/d/" + base.UID,
                   "icon": "external link"},
                  {"title": "jolt Fahrtenbuch", "type": "link", "url": "/d/jolt-logbook",
                   "icon": "external link"}],
    }


if __name__ == "__main__":
    dashboard = build()
    OUT.write_text(json.dumps(dashboard, indent=1, ensure_ascii=False), encoding="utf-8")
    if "--push" in sys.argv:
        position = sys.argv.index("--push")
        # The validation in push() substitutes ${session} and ${colour}; the
        # analysis dashboard has its own variables.
        substitutions = {"${tx}": "outside_c", "${ty}": "kwh100", "${wx}": "speed_kmh",
                         "${wy}": "kwh100", "${minkm}": "3", "${tx:text}": "x", "${ty:text}": "y",
                         "${wx:text}": "x", "${wy:text}": "y"}
        base.EXTRA_SUBSTITUTIONS = substitutions
        base.push(sys.argv[position + 1], sys.argv[position + 2], dashboard)
