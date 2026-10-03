-- ============================================================================
-- Agentic AI Network Investigation Challenge - Database Schema
-- ============================================================================
-- This schema is created automatically when the Postgres container starts
-- (see podman-compose.yml). You should NOT need to modify this file, but you
-- are welcome to add your own indexes/views if it helps your agent.
--
-- Four tables, by design -- this challenge is about agent development, not
-- data modeling, so the schema is intentionally lean. Topology and "prior
-- ticket" context that might otherwise live in separate tables is folded
-- into network_devices.notes and device_syslogs text where relevant; see
-- docs/schema_reference.md.
-- ============================================================================

-- 1. Anomalies detected by upstream ML models (the trigger for an investigation)
CREATE TABLE IF NOT EXISTS detected_anomalies (
    anomaly_id           TEXT PRIMARY KEY,
    severity             TEXT CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    model_output          JSONB,           -- rich, model-specific JSON payload (see docs/schema_reference.md)
    anomaly_date          DATE
);

-- 2. Device inventory (also carries lightweight topology / WAN-circuit info
--    as plain columns, since there's no separate topology table)
CREATE TABLE IF NOT EXISTS network_devices (
    device_id          TEXT PRIMARY KEY,
    hostname            TEXT UNIQUE NOT NULL,
    mgmt_ip              TEXT,
    device_type          TEXT,      -- router | switch | firewall | sdwan_edge
    vendor                TEXT,
    model                  TEXT,
    role                    TEXT,   -- core_router | core_switch | internet_zone_router | firewall | sdwan_branch_edge
    site_code                TEXT,
    site_name                  TEXT,
    city                          TEXT,
    state                          TEXT,
    region                        TEXT,
    install_date                  DATE,
    os_version                    TEXT,
    status                        TEXT,
    wan_provider                  TEXT,  -- SD-WAN / WAN-facing devices only, else NULL/empty
    wan_circuit_id                TEXT,
    wan_circuit_group              TEXT, -- devices sharing a group share underlying provider infrastructure
    notes                          TEXT  -- plain-language adjacency/topology context (which interface
                                          -- connects to which peer device) -- read this before assuming
                                          -- two devices are unrelated just because there's no join key
);

-- 3. Device-level hourly time series telemetry. This single wide table
--    carries both general health metrics and explicit diagnostic counters
--    (interface errors/flaps, policy denies, SD-WAN path quality) so
--    root-cause signal is directly queryable, not something you have to
--    infer purely from log text.
CREATE TABLE IF NOT EXISTS device_telemetry (
    device_id                TEXT REFERENCES network_devices(device_id),
    timestamp                  TIMESTAMP,
    cpu_utilization_pct          NUMERIC,
    memory_utilization_pct        NUMERIC,
    temperature_celsius            NUMERIC,
    active_sessions                  INTEGER,  -- firewalls / SD-WAN edges only, else NULL
    bgp_established_peers              INTEGER,  -- BGP-speaking devices only, else NULL
    interfaces_up_ratio                  NUMERIC,  -- 1.0 = all monitored interfaces up
    interface_error_count                  INTEGER,  -- only populated for devices with a monitored uplink (see docs)
    interface_flap_count                     INTEGER,  -- only populated for devices with a monitored uplink
    policy_deny_count                          INTEGER,  -- firewalls only, else NULL
    latency_ms                                   NUMERIC,  -- SD-WAN edges only, else NULL
    jitter_ms                                      NUMERIC,  -- SD-WAN edges only, else NULL
    packet_loss_pct                                  NUMERIC,  -- SD-WAN edges only, else NULL
    PRIMARY KEY (device_id, timestamp)
);

-- 4. Raw device system logs -- this is where exact event narratives,
--    ticket/change references, rule names, and resolution notes live.
CREATE TABLE IF NOT EXISTS device_syslogs (
    log_id         TEXT PRIMARY KEY,
    device_id      TEXT REFERENCES network_devices(device_id),
    timestamp       TIMESTAMP,
    severity         TEXT,   -- info | warning | error | critical
    message_type     TEXT,   -- interface | routing | bgp | vpn | policy | threat | system | auth | ha | sdwan | bfd
    message           TEXT
);

-- Helpful indexes
CREATE INDEX IF NOT EXISTS idx_telemetry_device_ts ON device_telemetry (device_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_syslogs_device_ts ON device_syslogs (device_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_anomalies_date ON detected_anomalies (anomaly_date);
CREATE INDEX IF NOT EXISTS idx_anomalies_severity ON detected_anomalies (severity);
