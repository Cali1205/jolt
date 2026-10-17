-- Read-only access for Grafana. Run once as the jolt user:
--   docker exec -i jolt-db psql -U jolt -d jolt -v pw="'<password>'" < tools/grafana/setup.sql
-- The views flatten the raw values of the dongle (JSON) into columns, so the
-- dashboard needs no JSON handling. The role sees only these views, not the
-- tables (no logger tokens, no sessions, no push subscriptions).

CREATE OR REPLACE VIEW grafana_points AS
SELECT p.id,
       p.session_id,
       p.timestamp AS time,
       p.lat, p.lon, p.soc, p.km_on_route, p.plan_soc,
       r.v->>'speed_kmh' AS speed_text,
       (r.v->>'speed_kmh')::float        AS speed_kmh,
       (r.v->>'odometer_km')::float      AS odometer_km,
       (r.v->>'voltage_v')::float        AS voltage_v,
       (r.v->>'current_a')::float        AS current_a,
       -- discharge positive, regeneration negative
       -(r.v->>'voltage_v')::float * (r.v->>'current_a')::float / 1000 AS power_kw,
       (r.v->>'discharge_kwh')::float    AS discharge_kwh,
       (r.v->>'charged_kwh')::float      AS charged_kwh,
       (r.v->>'aux_load_kw')::float      AS aux_load_kw,
       (r.v->>'hv_aux_current_a')::float AS hv_aux_current_a,
       (r.v->>'compressor_w')::float     AS compressor_w,
       (r.v->>'torque_rear_nm')::float   AS torque_rear_nm,
       (r.v->>'motor_rpm')::float        AS motor_rpm,
       (r.v->>'elevation_m')::float      AS elevation_m,
       (r.v->>'soc_fine_pct')::float     AS soc_fine_pct,
       (r.v->>'energy_content_kwh')::float AS energy_content_kwh,
       (r.v->>'range_km')::float         AS range_km,
       (r.v->>'cell_min_v')::float       AS cell_min_v,
       (r.v->>'cell_max_v')::float       AS cell_max_v,
       (r.v->>'batt_temp_min_c')::float  AS batt_temp_min_c,
       (r.v->>'batt_temp_max_c')::float  AS batt_temp_max_c,
       COALESCE((r.v->>'outside_temp_c')::float,
                (r.v->>'outside_t_cabin_c')::float) AS outside_temp_c,
       (r.v->>'inside_temp_c')::float    AS inside_temp_c
FROM live_points p
CROSS JOIN LATERAL (SELECT CASE WHEN json_typeof(p.raw_values) = 'object'
                                THEN p.raw_values::jsonb END AS v) r;

-- One row per recording: distance and net energy from the dongle counters
-- (the odometer has whole-kilometre resolution, so very short trips are
-- coarse), plus the average outside temperature.
CREATE OR REPLACE VIEW grafana_sessions AS
SELECT s.id,
       s.started_at AS time,
       s.ended_at,
       s.running,
       s.consumption_factor,
       v.name AS vehicle,
       lp.last_point,
       lp.last_point - s.started_at AS duration,
       lp.points,
       lp.km_gps,
       lp.km_odometer,
       lp.net_kwh,
       CASE WHEN lp.km_odometer >= 3
            THEN 100 * lp.net_kwh / lp.km_odometer END AS kwh_per_100km,
       lp.soc_start, lp.soc_end,
       lp.outside_c,
       lp.speed_max_kmh,
       lp.aux_avg_kw
FROM live_sessions s
JOIN trips t ON t.id = s.trip_id
LEFT JOIN vehicles v ON v.id = t.vehicle_id
LEFT JOIN LATERAL (
    SELECT count(*) AS points,
           max(time) AS last_point,
           max(km_on_route) AS km_gps,
           max(odometer_km) - min(odometer_km) AS km_odometer,
           (max(discharge_kwh) - min(discharge_kwh))
             - (max(charged_kwh) - min(charged_kwh)) AS net_kwh,
           (array_agg(soc ORDER BY time) FILTER (WHERE soc IS NOT NULL))[1] AS soc_start,
           (array_agg(soc ORDER BY time DESC) FILTER (WHERE soc IS NOT NULL))[1] AS soc_end,
           avg(outside_temp_c) AS outside_c,
           max(speed_kmh) AS speed_max_kmh,
           avg(aux_load_kw) AS aux_avg_kw
    FROM grafana_points WHERE session_id = s.id) lp ON true;

-- Trip level for the correlation dashboard: one row per recording with
-- derived indicators. "aux" is the auxiliary load (heating, air conditioning,
-- 12 V), "drive" is what remains of the consumption without it.
CREATE OR REPLACE VIEW grafana_trips AS
SELECT s.id, s.time,
       s.km_odometer AS km,
       s.kwh_per_100km AS kwh100,
       s.outside_c,
       s.soc_start,
       s.speed_max_kmh AS speed_max,
       s.aux_avg_kw AS aux_kw,
       extract(epoch FROM s.duration) / 60 AS duration_min,
       m.speed_avg,
       s.aux_avg_kw / nullif(m.speed_avg, 0) * 100 AS aux_kwh100,
       s.kwh_per_100km - s.aux_avg_kw / nullif(m.speed_avg, 0) * 100 AS drive_kwh100,
       m.stop_share_pct,
       m.regen_pct,
       m.ascent_per_km,
       m.batt_temp_c
FROM grafana_sessions s
LEFT JOIN LATERAL (
    SELECT avg(speed_kmh) FILTER (WHERE speed_kmh > 3) AS speed_avg,
           100.0 * count(*) FILTER (WHERE speed_kmh < 1) / nullif(count(speed_kmh), 0) AS stop_share_pct,
           100.0 * (max(charged_kwh) - min(charged_kwh))
                 / nullif(max(discharge_kwh) - min(discharge_kwh), 0) AS regen_pct,
           sum(greatest(climb, 0)) / nullif(s.km_odometer, 0) AS ascent_per_km,
           avg(batt_temp_max_c) AS batt_temp_c
    FROM (SELECT speed_kmh, charged_kwh, discharge_kwh, batt_temp_max_c,
                 elevation_m - lag(elevation_m) OVER (ORDER BY time) AS climb
          FROM grafana_points
          WHERE session_id = s.id) q) m ON true;

-- Windows of 60 s over all recordings (only while driving, with at least two
-- measured points), for the scatter plots of the analysis dashboard. The
-- outside temperature is the average of the whole trip: the dongle reports
-- it only every few minutes.
CREATE OR REPLACE VIEW grafana_windows AS
SELECT w.session_id, w.time, w.speed_kmh, w.power_kw, w.aux_kw, w.batt_temp_c,
       s.outside_c,
       CASE WHEN w.speed_kmh > 10 THEN w.power_kw / w.speed_kmh * 100 END AS kwh100,
       CASE WHEN w.speed_kmh > 10 THEN w.aux_kw / w.speed_kmh * 100 END AS aux_kwh100,
       CASE WHEN w.speed_kmh > 10 THEN (w.power_kw - w.aux_kw) / w.speed_kmh * 100 END AS drive_kwh100,
       CASE WHEN w.speed_kmh > 10 AND w.secs >= 20
            THEN (w.elev_last - w.elev_first) / (w.speed_kmh / 3.6 * w.secs) * 100 END AS slope_pct
FROM (SELECT session_id,
             date_bin('60 seconds', time, TIMESTAMP '2000-01-01') AS time,
             avg(speed_kmh) AS speed_kmh, avg(power_kw) AS power_kw,
             avg(aux_load_kw) AS aux_kw, avg(batt_temp_max_c) AS batt_temp_c,
             (array_agg(elevation_m ORDER BY time) FILTER (WHERE elevation_m IS NOT NULL))[1] AS elev_first,
             (array_agg(elevation_m ORDER BY time DESC) FILTER (WHERE elevation_m IS NOT NULL))[1] AS elev_last,
             extract(epoch FROM max(time) - min(time)) AS secs
      FROM grafana_points
      WHERE speed_kmh IS NOT NULL AND power_kw IS NOT NULL
      GROUP BY 1, 2 HAVING count(*) >= 2 AND avg(speed_kmh) > 5) w
JOIN grafana_sessions s ON s.id = w.session_id;

-- Role. The password comes from -v pw=... so it is not stored in the repo.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        CREATE ROLE grafana_ro LOGIN;
    END IF;
END $$;
ALTER ROLE grafana_ro PASSWORD :pw;
ALTER ROLE grafana_ro SET default_transaction_read_only = on;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON grafana_points, grafana_sessions, grafana_trips, grafana_windows TO grafana_ro;
