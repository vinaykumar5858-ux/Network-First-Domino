-- ============================================================================
-- Loads the pre-generated CSV seed files into the tables created by
-- 01_schema.sql. Runs automatically on first container start. You should not
-- need to touch this file.
--
-- Files are read from /seed_internal, a container-owned, world-readable copy
-- of the mounted /seed directory. The copy is made by the postgres service's
-- command wrapper in podman-compose.yml BEFORE this script runs, so seeding
-- succeeds regardless of the permissions the host seed files happen to carry
-- (e.g. owner-only 0600 files that the in-container postgres user could not
-- otherwise read).
-- ============================================================================

\copy detected_anomalies FROM '/seed_internal/detected_anomalies.csv' WITH (FORMAT csv, HEADER true);
\copy network_devices FROM '/seed_internal/network_devices.csv' WITH (FORMAT csv, HEADER true);
\copy device_telemetry FROM '/seed_internal/device_telemetry.csv' WITH (FORMAT csv, HEADER true);
\copy device_syslogs FROM '/seed_internal/device_syslogs.csv' WITH (FORMAT csv, HEADER true);

-- Sanity output visible in `docker compose logs postgres` on first boot
SELECT 'detected_anomalies' AS table_name, COUNT(*) FROM detected_anomalies
UNION ALL SELECT 'network_devices', COUNT(*) FROM network_devices
UNION ALL SELECT 'device_telemetry', COUNT(*) FROM device_telemetry
UNION ALL SELECT 'device_syslogs', COUNT(*) FROM device_syslogs;
