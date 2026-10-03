# Worked example - interface_flap anomaly `a1f0c8e2-1b44-4d90-9c31-000000000001`

### User

> Investigate anomaly a1f0c8e2-1b44-4d90-9c31-000000000001

<details><summary>Graph trace</summary>

```
route -> intent=investigate
start_investigation
investigator | get_device_details({"device": "FAIRVIEW-EDG01"}), get_device_details({"device": "stonebridge-edg01"}), get_device_syslogs({"end_time": "2026-07-08T08:00:00Z", "start_time": "2026-07-08T06:00:00Z", "max_rows": 50, "device": "FAIRVIEW-EDG01", "keyword": "link"}), get_device_syslogs({"end_time": "2026-07-08T08:00:00Z", "max_rows": 50, "start_time": "2026-07-08T06:00:00Z", "keyword": "link", "device": "stonebridge-edg01"})
execute_tools
investigator | get_device_syslogs({"end_time": "2026-07-08T08:00:00Z", "start_time": "2026-07-08T06:00:00Z", "max_rows": 50, "device": "FAIRVIEW-EDG01"}), get_device_syslogs({"start_time": "2026-07-08T06:00:00Z", "end_time": "2026-07-08T08:00:00Z", "max_rows": 50, "device": "stonebridge-edg01"}), get_device_telemetry({"metric_filter": "xe-0/0/21", "device": "FAIRVIEW-EDG01", "end_time": "2026-07-08T08:00:00Z", "start_time": "2026-07-08T06:00:00Z"}), get_device_telemetry({"end_time": "2026-07-08T08:00:00Z", "device": "stonebridge-edg01", "start_time": "2026-07-08T06:00:00Z", "metric_filter": "xe-0/0/0"})
execute_tools
investigator | run_readonly_sql({"sql": "SELECT * FROM device_syslogs WHERE timestamp >= '2026-07-08T07:00:00Z' AND timestamp <= '2026-07-08T07:05:00Z' ORDER BY timestamp;"})
execute_tools
investigator | run_readonly_sql({"sql": "SELECT * FROM device_syslogs WHERE timestamp >= '2026-07-08T07:44:00Z' AND timestamp <= '2026-07-08T07:47:00Z' ORDER BY timestamp;"})
execute_tools
investigator
synthesize
```
</details>

### Agent

## Root Cause Analysis - anomaly `a1f0c8e2-1b44-4d90-9c31-000000000001`
**Summary:** Intermittent physical optical degradation and low Rx power caused the backbone uplink between FAIRVIEW-EDG01 (xe-0/0/21) and stonebridge-edg01 (xe-0/0/0) to flap twice on 2026-07-08. This physical link failure triggered OSPF session drops, localized routing reconvergences, and downstream BGP/IPSec rekey storms.

**Most likely root cause:** Intermittent optical signal degradation and low optical receive power on interface xe-0/0/21 of FAIRVIEW-EDG01 and xe-0/0/0 of stonebridge-edg01.  _(category: optics)_  
**Confidence:** HIGH - Syslog evidence explicitly provides the exact pre-failure symptom (Rx power dropping to -18.2 dBm against a -15.0 dBm threshold and high pre-FEC BER) preceding every physical link down event across both connected devices.  
**Affected device(s):** FAIRVIEW-EDG01, stonebridge-edg01  
**Affected interface(s):** xe-0/0/21, xe-0/0/0  
**Timeframe:** 2026-07-08T07:02:00Z -> 2026-07-08T07:46:30Z  

### Evidence
- [+] **device_syslogs**: At 2026-07-08 07:02:00, device FAIRVIEW-EDG01 logged: xe-0/0/21 optics: high pre-FEC BER detected (Rx power -18.2 dBm, threshold -15.0 dBm), signal degrading
- [+] **device_syslogs**: At 2026-07-08 07:02:05, device stonebridge-edg01 logged: xe-0/0/0: physical link DOWN (SNMP_LINK trap) - peer FAIRVIEW-EDG01 xe-0/0/21
- [+] **device_syslogs**: At 2026-07-08 07:02:30, device FAIRVIEW-EDG01 logged: xe-0/0/21: physical link DOWN (SNMP_LINK trap)
- [i] **device_syslogs**: At 2026-07-08 07:02:40, device FAIRVIEW-EDG01 logged: xe-0/0/21: physical link UP (SNMP_LINK trap)
- [i] **device_syslogs**: At 2026-07-08 07:02:45, device FAIRVIEW-EDG01 logged: OSPF neighbor 10.64.9.1 on xe-0/0/21 state changed to Down (KillNbr, hold time expired)
- [+] **device_syslogs**: At 2026-07-08 07:45:00, device FAIRVIEW-EDG01 logged: xe-0/0/21 optics: high pre-FEC BER detected (Rx power -18.2 dBm, threshold -15.0 dBm), signal degrading
- [+] **device_syslogs**: At 2026-07-08 07:45:05, device stonebridge-edg01 logged: xe-0/0/0: physical link DOWN (SNMP_LINK trap) - peer FAIRVIEW-EDG01 xe-0/0/21
- [+] **device_syslogs**: At 2026-07-08 07:45:30, device FAIRVIEW-EDG01 logged: xe-0/0/21: physical link DOWN (SNMP_LINK trap)

### Alternative hypotheses considered
- Configuration changes causing interface resets (ruled out as no config changes were logged)
- Upstream provider outage affecting WAN transit (ruled out as the issue is isolated to the direct interconnect between FAIRVIEW-EDG01 and stonebridge-edg01)

### Recommended next steps (not executed)
- Inspect and clean fiber patch cords and optical transceivers on FAIRVIEW-EDG01 xe-0/0/21 and stonebridge-edg01 xe-0/0/0
- Replace failing SFP+ optic modules or repair/replace the fiber run if cleaning does not resolve the Rx power deficit

### User

> What happened first - the errors or the link going down?

<details><summary>Graph trace</summary>

```
route -> intent=followup
start_qa
qa_agent
```
</details>

### Agent

The optical errors happened first. 

Specifically, at **07:02:00** (and again at **07:45:00**), FAIRVIEW-EDG01 logged an optical warning reporting high pre-FEC BER and low receive power (-18.2 dBm against a -15.0 dBm threshold). 

The physical link going down followed shortly after:
* **07:02:05**: stonebridge-edg01 logged the link going down.
* **07:02:30**: FAIRVIEW-EDG01 logged the link going down.

## Structured RCA (JSON)

```json
{
  "summary": "Intermittent physical optical degradation and low Rx power caused the backbone uplink between FAIRVIEW-EDG01 (xe-0/0/21) and stonebridge-edg01 (xe-0/0/0) to flap twice on 2026-07-08. This physical link failure triggered OSPF session drops, localized routing reconvergences, and downstream BGP/IPSec rekey storms.",
  "root_cause": "Intermittent optical signal degradation and low optical receive power on interface xe-0/0/21 of FAIRVIEW-EDG01 and xe-0/0/0 of stonebridge-edg01.",
  "root_cause_category": "optics",
  "affected_devices": [
    "FAIRVIEW-EDG01",
    "stonebridge-edg01"
  ],
  "affected_interfaces": [
    "xe-0/0/21",
    "xe-0/0/0"
  ],
  "timeframe_start": "2026-07-08T07:02:00Z",
  "timeframe_end": "2026-07-08T07:46:30Z",
  "evidence": [
    {
      "source": "device_syslogs",
      "observation": "At 2026-07-08 07:02:00, device FAIRVIEW-EDG01 logged: xe-0/0/21 optics: high pre-FEC BER detected (Rx power -18.2 dBm, threshold -15.0 dBm), signal degrading",
      "relation": "supports"
    },
    {
      "source": "device_syslogs",
      "observation": "At 2026-07-08 07:02:05, device stonebridge-edg01 logged: xe-0/0/0: physical link DOWN (SNMP_LINK trap) - peer FAIRVIEW-EDG01 xe-0/0/21",
      "relation": "supports"
    },
    {
      "source": "device_syslogs",
      "observation": "At 2026-07-08 07:02:30, device FAIRVIEW-EDG01 logged: xe-0/0/21: physical link DOWN (SNMP_LINK trap)",
      "relation": "supports"
    },
    {
      "source": "device_syslogs",
      "observation": "At 2026-07-08 07:02:40, device FAIRVIEW-EDG01 logged: xe-0/0/21: physical link UP (SNMP_LINK trap)",
      "relation": "context"
    },
    {
      "source": "device_syslogs",
      "observation": "At 2026-07-08 07:02:45, device FAIRVIEW-EDG01 logged: OSPF neighbor 10.64.9.1 on xe-0/0/21 state changed to Down (KillNbr, hold time expired)",
      "relation": "context"
    },
    {
      "source": "device_syslogs",
      "observation": "At 2026-07-08 07:45:00, device FAIRVIEW-EDG01 logged: xe-0/0/21 optics: high pre-FEC BER detected (Rx power -18.2 dBm, threshold -15.0 dBm), signal degrading",
      "relation": "supports"
    },
    {
      "source": "device_syslogs",
      "observation": "At 2026-07-08 07:45:05, device stonebridge-edg01 logged: xe-0/0/0: physical link DOWN (SNMP_LINK trap) - peer FAIRVIEW-EDG01 xe-0/0/21",
      "relation": "supports"
    },
    {
      "source": "device_syslogs",
      "observation": "At 2026-07-08 07:45:30, device FAIRVIEW-EDG01 logged: xe-0/0/21: physical link DOWN (SNMP_LINK trap)",
      "relation": "supports"
    }
  ],
  "alternative_hypotheses": [
    "Configuration changes causing interface resets (ruled out as no config changes were logged)",
    "Upstream provider outage affecting WAN transit (ruled out as the issue is isolated to the direct interconnect between FAIRVIEW-EDG01 and stonebridge-edg01)"
  ],
  "confidence": "high",
  "confidence_rationale": "Syslog evidence explicitly provides the exact pre-failure symptom (Rx power dropping to -18.2 dBm against a -15.0 dBm threshold and high pre-FEC BER) preceding every physical link down event across both connected devices.",
  "evidence_gaps": [],
  "recommended_next_steps": [
    "Inspect and clean fiber patch cords and optical transceivers on FAIRVIEW-EDG01 xe-0/0/21 and stonebridge-edg01 xe-0/0/0",
    "Replace failing SFP+ optic modules or repair/replace the fiber run if cleaning does not resolve the Rx power deficit"
  ],
  "calibration_notes": [],
  "evidence_sources_with_data": [
    "get_device_details",
    "get_device_syslogs",
    "get_device_telemetry",
    "run_readonly_sql"
  ],
  "anomaly_id": "a1f0c8e2-1b44-4d90-9c31-000000000001",
  "tool_calls": [
    {
      "tool": "get_device_details",
      "args": {
        "device": "FAIRVIEW-EDG01"
      },
      "has_data": true
    },
    {
      "tool": "get_device_details",
      "args": {
        "device": "stonebridge-edg01"
      },
      "has_data": true
    },
    {
      "tool": "get_device_syslogs",
      "args": {
        "end_time": "2026-07-08T08:00:00Z",
        "start_time": "2026-07-08T06:00:00Z",
        "max_rows": 50,
        "device": "FAIRVIEW-EDG01",
        "keyword": "link"
      },
      "has_data": true
    },
    {
      "tool": "get_device_syslogs",
      "args": {
        "end_time": "2026-07-08T08:00:00Z",
        "max_rows": 50,
        "start_time": "2026-07-08T06:00:00Z",
        "keyword": "link",
        "device": "stonebridge-edg01"
      },
      "has_data": true
    },
    {
      "tool": "get_device_syslogs",
      "args": {
        "end_time": "2026-07-08T08:00:00Z",
        "start_time": "2026-07-08T06:00:00Z",
        "max_rows": 50,
        "device": "FAIRVIEW-EDG01"
      },
      "has_data": true
    },
    {
      "tool": "get_device_syslogs",
      "args": {
        "start_time": "2026-07-08T06:00:00Z",
        "end_time": "2026-07-08T08:00:00Z",
        "max_rows": 50,
        "device": "stonebridge-edg01"
      },
      "has_data": true
    },
    {
      "tool": "get_device_telemetry",
      "args": {
        "metric_filter": "xe-0/0/21",
        "device": "FAIRVIEW-EDG01",
        "end_time": "2026-07-08T08:00:00Z",
        "start_time": "2026-07-08T06:00:00Z"
      },
      "has_data": true
    },
    {
      "tool": "get_device_telemetry",
      "args": {
        "end_time": "2026-07-08T08:00:00Z",
        "device": "stonebridge-edg01",
        "start_time": "2026-07-08T06:00:00Z",
        "metric_filter": "xe-0/0/0"
      },
      "has_data": true
    },
    {
      "tool": "run_readonly_sql",
      "args": {
        "sql": "SELECT * FROM device_syslogs WHERE timestamp >= '2026-07-08T07:00:00Z' AND timestamp <= '2026-07-08T07:05:00Z' ORDER BY timestamp;"
      },
      "has_data": true
    },
    {
      "tool": "run_readonly_sql",
      "args": {
        "sql": "SELECT * FROM device_syslogs WHERE timestamp >= '2026-07-08T07:44:00Z' AND timestamp <= '2026-07-08T07:47:00Z' ORDER BY timestamp;"
      },
      "has_data": true
    }
  ]
}
```
