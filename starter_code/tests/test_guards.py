import pytest

from agent.db_access import validate_select
from agent.graph import UUID_RE, calibrate, result_has_data


@pytest.mark.parametrize("sql", [
    "SELECT * FROM device_syslogs",
    "with x as (select 1) select * from x",
    "select * from device_syslogs where message ilike '%delete%';",
])
def test_select_allowed(sql):
    assert validate_select(sql)


@pytest.mark.parametrize("sql", [
    "delete from device_syslogs",
    "select 1; drop table network_devices",
    "update network_devices set role='x'",
    "select pg_sleep(100)",
    "",
])
def test_writes_and_tricks_blocked(sql):
    with pytest.raises(ValueError):
        validate_select(sql)


def test_uuid_detection():
    assert UUID_RE.findall("please investigate a1f0c8e2-1b44-4d90-9c31-000000000001 now")


def test_result_has_data():
    assert not result_has_data('{"row_count": 0}')
    assert not result_has_data('{"error": "nope"}')
    assert result_has_data('{"total_messages": 3, "messages": []}')


def _report(conf, relation="supports"):
    return {"confidence": conf, "root_cause": "bad optic",
            "evidence": [{"source": "s", "observation": "o", "relation": relation}]}


def test_calibration_no_data_means_insufficient():
    r = calibrate(_report("high"), [{"tool": "get_device_syslogs", "has_data": False}])
    assert r["confidence"] == "insufficient"
    assert r["root_cause"].startswith("Undetermined")


def test_calibration_single_source_caps_at_medium():
    r = calibrate(_report("high"), [{"tool": "get_device_syslogs", "has_data": True}])
    assert r["confidence"] == "medium"


def test_calibration_keeps_well_supported_high():
    log = [{"tool": "get_device_syslogs", "has_data": True}, {"tool": "get_device_telemetry", "has_data": True}]
    assert calibrate(_report("high"), log)["confidence"] == "high"


def test_calibration_no_supporting_evidence_caps_low():
    log = [{"tool": "get_device_syslogs", "has_data": True}, {"tool": "get_device_telemetry", "has_data": True}]
    assert calibrate(_report("high", "context"), log)["confidence"] == "low"
