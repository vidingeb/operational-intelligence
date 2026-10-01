"""Deterministic daily estate health collection, rules, and Markdown rendering."""
import asyncio
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

SCHEMA_VERSION = 1
COLLECTOR_VERSION = "daily-health-v1"
STALE_RESTORE_POINT_HOURS = 48

SCORE_RULES = {
    "critical_alarm": 50,
    "immediate_alert": 40,
    "critical_alert": 35,
    "failed_backup_session": 30,
    "no_restore_point_powered_on": 25,
    "stale_restore_point": 15,
    "old_snapshot": 10,
    "tools_problem_powered_on": 8,
    "high_host_memory": 12,
    "high_host_cpu": 10,
    "low_datastore_free": 20,
}

Progress = Callable[[str, str, dict], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(data: Any, keys=()) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []


def envelope(source: str, query: str, data: Any, collected_at: str, window: dict,
             *, row_keys=(), total: Optional[int] = None,
             complete: Optional[bool] = None) -> dict:
    error = data.get("error") if isinstance(data, dict) else None
    rows = _rows(data, row_keys)
    truncated = bool(isinstance(data, dict) and data.get("truncated"))
    if total is None:
        total = len(rows)
    if complete is False:
        truncated = total > len(rows) or truncated
    status = "failed" if error is not None else ("truncated" if truncated else "complete")
    return {
        "source": source,
        "query": query,
        "collected_at": collected_at,
        "window": window,
        "status": status,
        "truncated": truncated,
        "records_examined": len(rows),
        "records_returned": len(rows),
        "error": str(error) if error is not None else None,
        "data": data,
    }


def capability_envelopes(collected_at: str, window: dict) -> dict:
    return {
        "dell_switch_telemetry": {
            "source": "Dell switch telemetry",
            "query": "direct switch health and interface telemetry",
            "collected_at": collected_at,
            "window": window,
            "status": "not_configured",
            "truncated": False,
            "records_examined": 0,
            "records_returned": 0,
            "error": None,
            "data": {
                "limitation": "No direct Dell switch telemetry source is configured."
            },
        },
        "server_hardware_health": {
            "source": "Direct server hardware",
            "query": "BMC/iDRAC sensor and hardware health",
            "collected_at": collected_at,
            "window": window,
            "status": "not_configured",
            "truncated": False,
            "records_examined": 0,
            "records_returned": 0,
            "error": None,
            "data": {
                "limitation": (
                    "No direct BMC/iDRAC source is configured. IPMI messages in "
                    "Logs are log evidence only, not complete hardware health."
                )
            },
        },
        "veeam_repository_capacity": {
            "source": "Veeam repository",
            "query": "backup repository capacity",
            "collected_at": collected_at,
            "window": window,
            "status": "unsupported",
            "truncated": False,
            "records_examined": 0,
            "records_returned": 0,
            "error": None,
            "data": {
                "limitation": (
                    "The configured Veeam API wrapper does not expose repository "
                    "capacity. Datastore/vSAN capacity is not a substitute."
                )
            },
        },
    }


def _status_from_required(values: list[str]) -> str:
    if any(value in ("UNKNOWN", "FAILED") for value in values):
        return "UNKNOWN"
    if any(value in ("CRITICAL", "WARNING") for value in values):
        return "CRITICAL" if "CRITICAL" in values else "WARNING"
    return "HEALTHY"


def _severity(record: dict) -> str:
    return str(
        record.get("alertLevel")
        or record.get("severity")
        or record.get("overall_status")
        or "UNKNOWN"
    ).upper()


def _normal_name(value: Any) -> str:
    return str(value or "").strip().lower().split(".", 1)[0]


def _tools_problem(vm: dict) -> bool:
    if str(vm.get("power_state") or "").lower() != "poweredon":
        return False
    status = str(vm.get("vmware_tools_status") or "").lower()
    return status in {
        "guesttoolsnotrunning",
        "guesttoolsnotinstalled",
        "guesttoolssupportedold",
        "notrunning",
        "notinstalled",
        "outdated",
    }


def _backup_classification(vms: list, protected: list, failed_sessions: list) -> list:
    protected_by_name = {_normal_name(item.get("name")): item for item in protected}
    failed_names = " ".join(str(item.get("name") or "").lower() for item in failed_sessions)
    out = []
    for vm in vms:
        name = str(vm.get("name") or "")
        power = str(vm.get("power_state") or "unknown")
        item = protected_by_name.get(_normal_name(name))
        explicitly_excluded = bool(
            vm.get("backup_excluded")
            or vm.get("intentionally_excluded")
            or (item and (item.get("backup_excluded") or item.get("intentionally_excluded")))
        )
        if explicitly_excluded:
            classification = "intentionally_excluded"
            wording = "Explicit exclusion evidence observed."
        elif power.lower() != "poweredon":
            classification = "powered_off"
            wording = "Powered off; review protection intent before changing backup policy."
        elif item is None:
            classification = "unknown"
            wording = "No Veeam roster evidence; review protection intent."
        elif (item.get("restore_points") or 0) <= 0:
            classification = (
                "failed_job_or_session"
                if _normal_name(name) and _normal_name(name) in failed_names
                else "no_restore_point"
            )
            wording = "No restore point; review protection intent and job evidence."
        elif item.get("newest_restore_point_age_hours") is None:
            classification = "unknown"
            wording = "Restore-point age unavailable; protection freshness is unknown."
        elif float(item["newest_restore_point_age_hours"]) > STALE_RESTORE_POINT_HOURS:
            classification = "stale_restore_point"
            wording = "Restore point is stale; review protection intent and job execution."
        else:
            classification = "recent_restore_point"
            wording = "Recent restore point observed."
        out.append({
            "vm": name,
            "power_state": power,
            "classification": classification,
            "wording": wording,
            "restore_points": item.get("restore_points") if item else None,
            "newest_restore_point_age_hours": (
                item.get("newest_restore_point_age_hours") if item else None
            ),
        })
    return out


def _score_objects(vms: list, backup: list, snapshots: list, alarms: list,
                   hosts: list, datastores: list) -> list:
    scores = defaultdict(lambda: {"score": 0, "breakdown": []})

    def add(name: str, rule: str, evidence: str):
        if not name:
            return
        points = SCORE_RULES[rule]
        scores[name]["score"] += points
        scores[name]["breakdown"].append({
            "rule": rule, "points": points, "evidence": evidence
        })

    vm_by_name = {_normal_name(vm.get("name")): vm for vm in vms}
    for item in backup:
        if item["classification"] == "no_restore_point":
            add(item["vm"], "no_restore_point_powered_on", "Powered-on VM has no restore point")
        elif item["classification"] == "failed_job_or_session":
            add(item["vm"], "failed_backup_session", "No restore point and matching failed session")
        elif item["classification"] == "stale_restore_point":
            add(item["vm"], "stale_restore_point", "Newest restore point is stale")

    for vm in vms:
        if _tools_problem(vm):
            tools = str(vm.get("vmware_tools_status") or "").lower()
            add(vm.get("name"), "tools_problem_powered_on", f"Tools status: {tools}")

    for snapshot in snapshots:
        add(snapshot.get("vm"), "old_snapshot", f"Snapshot age {snapshot.get('age_days')} days")

    for alarm in alarms:
        severity = _severity(alarm)
        if severity in ("RED", "CRITICAL"):
            add(alarm.get("entity"), "critical_alarm", str(alarm.get("alarm") or "critical alarm"))

    for host in hosts:
        if float(host.get("memory_usage_percent") or 0) >= 90:
            add(host.get("name"), "high_host_memory", "Memory utilization >= 90%")
        if float(host.get("cpu_usage_percent") or 0) >= 90:
            add(host.get("name"), "high_host_cpu", "CPU utilization >= 90%")

    for datastore in datastores:
        if 100 - float(datastore.get("used_percent") or 0) < 10:
            add(datastore.get("name"), "low_datastore_free", "Free capacity below 10%")

    return [
        {"object": name, **details}
        for name, details in sorted(
            scores.items(), key=lambda item: (-item[1]["score"], item[0].lower())
        )[:10]
    ]


def correlate(logs: list, failed_sessions: list) -> list:
    """Only correlate normalized same-object evidence within an overlapping window."""
    correlations = []
    for session in failed_sessions:
        session_name = _normal_name(session.get("name"))
        ended = session.get("ended")
        if not session_name or not ended:
            continue
        try:
            session_time = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
        except ValueError:
            continue
        for event in logs:
            text = str(event.get("text") or "")
            lowered = text.lower()
            if "firewallrulemaskevent" in lowered:
                continue
            if not any(category in lowered for category in (
                "backup", "veeam", "transport", "repository", "restore point"
            )):
                continue
            object_name = _normal_name(event.get("host"))
            if object_name != session_name:
                continue
            raw_time = event.get("time") or event.get("timestamp")
            try:
                event_time = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if abs((event_time - session_time).total_seconds()) <= 3600:
                correlations.append({
                    "label": "Possible correlation",
                    "object": session.get("name"),
                    "confidence": "medium",
                    "evidence": (
                        "Backup failure and compatible log event refer to the same "
                        "normalized object within one hour."
                    ),
                })
                break
    return correlations


def analyze(envelopes: dict, previous: Optional[dict] = None) -> dict:
    data = {key: value.get("data") for key, value in envelopes.items()}
    vms = _rows(data.get("vcenter_vms"), ())
    hosts = _rows(data.get("vcenter_hosts"), ())
    host_usage = _rows(data.get("vcenter_host_usage"), ())
    datastores = _rows(data.get("vcenter_datastores"), ())
    alarms = _rows(data.get("vcenter_alarms"), ())
    snapshots = _rows(data.get("vcenter_snapshots"), ())
    ops_alerts = _rows(data.get("vcf_ops_alerts"), ("alerts",))
    logs = _rows(data.get("logs_errors"), ("events",))
    flows = data.get("vcf_networks_flows") or {}
    network_alerts = _rows(data.get("vcf_networks_alerts"), ("results",))
    sessions = _rows(data.get("veeam_sessions"), ("sessions",))
    protected = _rows(data.get("veeam_protected"), ("objects",))
    jobs = _rows(data.get("veeam_jobs"), ("jobs",))

    power_counts = Counter(str(vm.get("power_state") or "unknown") for vm in vms)
    alarm_counts = Counter(_severity(item) for item in alarms)
    ops_counts = Counter(_severity(item) for item in ops_alerts)
    session_counts = Counter(str(item.get("result") or "Unknown") for item in sessions)
    failed_sessions = [
        item for item in sessions
        if str(item.get("result") or "").lower() not in ("success", "")
    ]
    backup = _backup_classification(vms, protected, failed_sessions)
    backup_counts = Counter(item["classification"] for item in backup)
    powered_on_tools_problems = [vm for vm in vms if _tools_problem(vm)]

    host_dimensions = {
        "inventory": "HEALTHY" if envelopes["vcenter_hosts"]["status"] == "complete" else "UNKNOWN",
        "resource_usage": "HEALTHY" if envelopes["vcenter_host_usage"]["status"] == "complete" else "UNKNOWN",
        "storage_capacity": (
            "WARNING" if any(float(ds.get("used_percent") or 0) >= 90 for ds in datastores)
            else "HEALTHY" if envelopes["vcenter_datastores"]["status"] == "complete"
            else "UNKNOWN"
        ),
        "storage_latency_path": "UNKNOWN",
        "network_hardware": "UNKNOWN",
        "server_hardware": "UNKNOWN",
    }
    host_status = _status_from_required(list(host_dimensions.values()))

    capacity = {
        "host_cpu_total_mhz": sum(float(item.get("cpu_total_mhz") or 0) for item in host_usage),
        "host_cpu_used_mhz": sum(float(item.get("cpu_usage_mhz") or 0) for item in host_usage),
        "host_memory_total_mb": sum(float(item.get("memory_total_mb") or 0) for item in host_usage),
        "host_memory_used_mb": sum(float(item.get("memory_usage_mb") or 0) for item in host_usage),
        "datastore_total_gb": sum(float(item.get("capacity_gb") or 0) for item in datastores),
        "datastore_used_gb": sum(float(item.get("used_gb") or 0) for item in datastores),
        "datastore_free_gb": sum(float(item.get("free_gb") or 0) for item in datastores),
        "veeam_repository_capacity": None,
    }

    findings = _score_objects(vms, backup, snapshots, alarms, host_usage, datastores)
    current_finding_ids = [
        f"{item['object']}:{part['rule']}" for item in findings for part in item["breakdown"]
    ]
    finding_scores = {item["object"]: item["score"] for item in findings}
    if previous is None:
        trends = {"baseline": False, "message": "No comparable baseline exists; this is the first run."}
    else:
        old_analysis = previous.get("analysis") or previous
        old = set(old_analysis.get("finding_ids") or [])
        new = set(current_finding_ids)
        old_scores = old_analysis.get("finding_scores") or {}
        shared_objects = set(old_scores) & set(finding_scores)
        trends = {
            "baseline": True,
            "new": sorted(new - old),
            "resolved": sorted(old - new),
            "worsened": sorted(
                name for name in shared_objects
                if finding_scores[name] > old_scores[name]
            ),
            "improved": sorted(
                name for name in shared_objects
                if finding_scores[name] < old_scores[name]
            ),
            "unchanged": sorted(new & old),
            "message": "Compared with the previous deterministic daily-health snapshot.",
        }

    source_status = {key: value["status"] for key, value in envelopes.items()}
    aggregate = _status_from_required([
        host_status,
        "UNKNOWN" if any(status in ("failed", "truncated", "partial") for status in source_status.values())
        else "HEALTHY",
        "CRITICAL" if alarm_counts.get("CRITICAL") or alarm_counts.get("RED") else "HEALTHY",
        "WARNING" if failed_sessions else "HEALTHY",
    ])

    return {
        "aggregate_status": aggregate,
        "host_status": host_status,
        "host_dimensions": host_dimensions,
        "counts": {
            "hosts": len(hosts),
            "vms": len(vms),
            "vms_by_power_state": dict(sorted(power_counts.items())),
            "powered_on_tools_problems": len(powered_on_tools_problems),
            "old_snapshots": len(snapshots),
            "vcenter_alarms_by_severity": dict(sorted(alarm_counts.items())),
            "vcf_ops_alerts_by_severity": dict(sorted(ops_counts.items())),
            "backup_sessions_by_exact_result": dict(sorted(session_counts.items())),
            "unique_backup_jobs": len({
                str(item.get("name") or item.get("id"))
                for item in jobs if item.get("name") or item.get("id")
            }),
            "backup_classification": dict(sorted(backup_counts.items())),
            "network_alert_records": len(network_alerts),
        },
        "backup": backup,
        "flow_sampling": {
            "examined": flows.get("flows_examined"),
            "returned": len(flows.get("flows") or []),
            "matching": flows.get("flow_count"),
            "total_known": flows.get("total_flows_known"),
            "truncated": bool(flows.get("truncated")),
            "traffic_type_breakdown": flows.get("traffic_type_breakdown") or {},
        },
        "capacity": capacity,
        "top_10": findings,
        "correlations": correlate(logs, failed_sessions),
        "trends": trends,
        "finding_ids": current_finding_ids,
        "finding_scores": finding_scores,
        "source_status": source_status,
        "hosts": hosts,
        "host_usage": host_usage,
        "datastores": datastores,
        "logs": logs,
    }


def render(report: dict) -> str:
    a = report["analysis"]
    meta = report["metadata"]
    c = a["counts"]
    flow = a["flow_sampling"]
    lines = [
        "# Daily Infrastructure Health Report",
        "",
        f"- **Report ID:** `{report['report_id']}`",
        f"- **Collector/schema:** `{COLLECTOR_VERSION}` / `{SCHEMA_VERSION}`",
        f"- **Collected at:** {meta['collected_at']} UTC",
        f"- **Window:** {meta['window']['start']} to {meta['window']['end']} UTC",
        "",
        "## Executive dashboard",
        "",
        "| Area | Status | Evidence |",
        "|---|---|---|",
        f"| Estate | **{a['aggregate_status']}** | Conservative aggregate; missing required checks are UNKNOWN |",
        f"| Hosts | **{a['host_status']}** | {c['hosts']} hosts; hardware/network/path checks remain explicit |",
        f"| VMs | **{'WARNING' if c['powered_on_tools_problems'] else 'HEALTHY'}** | {c['vms']} VMs; {c['powered_on_tools_problems']} powered-on Tools problems |",
        f"| Backup | **{'WARNING' if c['backup_classification'].get('no_restore_point', 0) or c['backup_classification'].get('failed_job_or_session', 0) else 'HEALTHY'}** | Exact session and restore-point evidence below |",
        "",
        "## Priority findings",
        "",
    ]
    if a["top_10"]:
        lines += ["| Rank | Object | Score | Score breakdown |", "|---:|---|---:|---|"]
        for index, item in enumerate(a["top_10"], 1):
            breakdown = "; ".join(
                f"{part['rule']} +{part['points']} ({part['evidence']})"
                for part in item["breakdown"]
            )
            lines.append(f"| {index} | {item['object']} | {item['score']} | {breakdown} |")
    else:
        lines.append("No scored findings were observed in the available evidence.")

    usage = {item.get("name"): item for item in a["host_usage"]}
    lines += [
        "",
        "## Hosts",
        "",
        "| Host | Connection | CPU used/total MHz | Memory used/total MB |",
        "|---|---|---:|---:|",
    ]
    for host in a["hosts"]:
        item = usage.get(host.get("name"), {})
        lines.append(
            f"| {host.get('name')} | {host.get('connection_state', 'unknown')} | "
            f"{item.get('cpu_usage_mhz', 'unknown')} / {item.get('cpu_total_mhz', 'unknown')} | "
            f"{item.get('memory_usage_mb', 'unknown')} / {item.get('memory_total_mb', 'unknown')} |"
        )
    lines += ["", "**Required host dimensions:**"]
    lines += [f"- {key.replace('_', ' ')}: **{value}**" for key, value in a["host_dimensions"].items()]

    lines += [
        "",
        "## VM and Top 10 summary",
        "",
        f"- VMs by power state: `{json.dumps(c['vms_by_power_state'], sort_keys=True)}`",
        f"- Powered-on VMware Tools problems: **{c['powered_on_tools_problems']}**. "
        "Powered-off Tools-not-running records score zero.",
        f"- Old snapshots: **{c['old_snapshots']}**",
        "",
        "## Network and flow sampling",
        "",
        f"- Examined: **{flow['examined']}**; returned: **{flow['returned']}**; "
        f"matching after filters: **{flow['matching']}**; total known: **{flow['total_known']}**.",
        f"- Truncated: **{flow['truncated']}**. These are sampling/completeness fields, "
        "not interchangeable counts.",
        f"- Traffic types: `{json.dumps(flow['traffic_type_breakdown'], sort_keys=True)}`",
        "- Dell switch telemetry is not configured. Network Insight evidence does not "
        "replace direct switch health.",
        "",
        "## Logs",
        "",
        f"- Error records returned: **{len(a['logs'])}**.",
        "- IPMI messages, when present, are log evidence only and are not treated as "
        "complete server-hardware health.",
        "",
        "## Veeam and restore-point coverage",
        "",
        f"- Sessions by exact result: `{json.dumps(c['backup_sessions_by_exact_result'], sort_keys=True)}`",
        f"- Unique configured jobs: **{c['unique_backup_jobs']}** (jobs and sessions are separate counts).",
        f"- VM classifications: `{json.dumps(c['backup_classification'], sort_keys=True)}`",
        "- A missing restore point means review protection intent; it is not an automatic "
        "instruction to add the VM to backup.",
        "",
        "## Capacity",
        "",
        "| Capacity | Total | Used | Free |",
        "|---|---:|---:|---:|",
        f"| Host CPU (MHz) | {a['capacity']['host_cpu_total_mhz']:.0f} | {a['capacity']['host_cpu_used_mhz']:.0f} | "
        f"{a['capacity']['host_cpu_total_mhz'] - a['capacity']['host_cpu_used_mhz']:.0f} |",
        f"| Host memory (MB) | {a['capacity']['host_memory_total_mb']:.0f} | {a['capacity']['host_memory_used_mb']:.0f} | "
        f"{a['capacity']['host_memory_total_mb'] - a['capacity']['host_memory_used_mb']:.0f} |",
        f"| Datastores (GB) | {a['capacity']['datastore_total_gb']:.1f} | {a['capacity']['datastore_used_gb']:.1f} | "
        f"{a['capacity']['datastore_free_gb']:.1f} |",
        "| Veeam repository | unknown | unknown | unknown |",
        "",
        "Datastore/vSAN capacity is not presented as Veeam repository capacity.",
        "",
        "## Correlations",
        "",
    ]
    if a["correlations"]:
        for item in a["correlations"]:
            lines.append(
                f"- **{item['label']}** ({item['confidence']}): {item['object']} — {item['evidence']}"
            )
    else:
        lines.append("No rule-supported cross-source correlations were found.")

    lines += [
        "",
        "## Trends",
        "",
        a["trends"]["message"],
    ]
    if a["trends"]["baseline"]:
        for label in ("new", "resolved", "worsened", "improved", "unchanged"):
            values = a["trends"].get(label) or []
            lines.append(f"- {label.title()}: **{len(values)}**" + (
                f" — {', '.join(values)}" if values else ""
            ))
    lines += ["", "## Recommended actions", ""]
    if a["top_10"]:
        lines.append("- Review the highest-scoring observed findings and their evidence.")
    if c["backup_classification"].get("unknown", 0):
        lines.append("- Review protection intent for VMs absent from Veeam evidence.")
    lines.append("- Configure direct BMC/iDRAC and Dell switch telemetry before claiming full host health.")

    lines += [
        "",
        "## Data-source status",
        "",
        "| Source query | Status | Truncated | Records examined | Records returned | Error |",
        "|---|---|---:|---:|---:|---|",
    ]
    for key, item in report["sources"].items():
        lines.append(
            f"| {key} ({item['source']}) | {item['status']} | {item['truncated']} | "
            f"{item['records_examined']} | {item['records_returned']} | {item['error'] or ''} |"
        )
    return "\n".join(lines) + "\n"


async def collect(call_api, load_previous, save_snapshot, *,
                  hours: int = 24, flow_limit: int = 100,
                  progress: Optional[Progress] = None) -> dict:
    now = utc_now()
    window = {"start": iso(now - timedelta(hours=hours)), "end": iso(now)}
    collected_at = iso(now)

    async def emit_progress(stage, message, **details):
        if progress:
            await progress(stage, message, details)

    plan = {
        "vcenter_vms": ("vCenter", "all VMs", "vcenter_list_vms", {}, ()),
        "vcenter_hosts": ("vCenter", "all hosts", "vcenter_list_hosts", {}, ()),
        "vcenter_host_usage": ("vCenter", "host CPU/memory usage", "vcenter_host_usage", {}, ()),
        "vcenter_datastores": ("vCenter", "datastore capacity", "vcenter_datastores", {}, ()),
        "vcenter_alarms": ("vCenter", "active alarms", "vcenter_alarms", {}, ()),
        "vcenter_snapshots": ("vCenter", "snapshots older than 14 days", "vcenter_old_snapshots", {"days": 14}, ()),
        "vcf_ops_summary": ("VCF Operations", "environment summary", "ops_summary", {}, ()),
        "vcf_ops_alerts": ("VCF Operations", "active alerts", "ops_alerts", {"activeOnly": True, "pageSize": 200}, ("alerts",)),
        "logs_errors": ("VCF Operations for Logs", "error events", "logs_errors", {"hours": hours, "limit": 100}, ("events",)),
        "vcf_networks_flows": ("VCF Operations for Networks", "resolved flow inventory", "networks_flow_inventory", {"hours": hours, "limit": flow_limit}, ("flows",)),
        "vcf_networks_alerts": ("VCF Operations for Networks", "active network alerts", "networks_alerts", {}, ("results",)),
        "veeam_sessions": ("Veeam", "sessions by exact result", "veeam_failed_jobs", {"hours": hours, "failed_only": False, "limit": 200}, ("sessions",)),
        "veeam_protected": ("Veeam", "protected-object restore points", "veeam_protected", {}, ("objects",)),
        "veeam_jobs": ("Veeam", "configured jobs", "veeam_jobs", {"limit": 100}, ("jobs",)),
    }
    groups = [
        ("collecting_vcenter", [key for key in plan if key.startswith("vcenter_")]),
        ("collecting_operations", ["vcf_ops_summary", "vcf_ops_alerts", "logs_errors"]),
        ("collecting_networks", ["vcf_networks_flows", "vcf_networks_alerts"]),
        ("collecting_backup", ["veeam_sessions", "veeam_protected", "veeam_jobs"]),
    ]
    raw = {}
    started = time.perf_counter()
    for stage, keys in groups:
        await emit_progress(stage, stage.replace("_", " ").title(), queries=keys)
        results = await asyncio.gather(
            *[call_api(plan[key][2], plan[key][3]) for key in keys],
            return_exceptions=True,
        )
        for key, result in zip(keys, results):
            raw[key] = {"error": f"{type(result).__name__}: {result}"} if isinstance(result, Exception) else result

    envelopes = {}
    for key, (source, query, _tool, _args, row_keys) in plan.items():
        value = raw[key]
        total = None
        complete = None
        if key == "vcf_ops_alerts" and isinstance(value, dict):
            total = (value.get("pageInfo") or {}).get("totalCount")
            complete = total is None or total <= len(value.get("alerts") or [])
        elif key == "logs_errors" and isinstance(value, dict):
            total = value.get("event_count")
            complete = value.get("search_complete")
        elif key == "vcf_networks_flows" and isinstance(value, dict):
            total = value.get("total_flows_known")
            complete = not value.get("truncated")
        elif key == "vcf_networks_alerts" and isinstance(value, dict):
            total = value.get("total_count")
            complete = total is None or total <= len(value.get("results") or [])
        elif key == "veeam_protected" and isinstance(value, dict):
            total = value.get("reported_total")
            complete = value.get("complete")
        envelopes[key] = envelope(
            source, query, value, collected_at, window,
            row_keys=row_keys, total=total, complete=complete,
        )
        if key == "vcf_networks_flows" and isinstance(value, dict):
            envelopes[key]["records_examined"] = value.get("flows_examined", 0)
            envelopes[key]["records_returned"] = len(value.get("flows") or [])
        elif key == "logs_errors" and isinstance(value, dict):
            envelopes[key]["records_examined"] = value.get("event_count", 0)
        elif key == "veeam_protected" and isinstance(value, dict):
            envelopes[key]["records_examined"] = value.get("objects_known_to_veeam", 0)
    envelopes.update(capability_envelopes(collected_at, window))

    await emit_progress("analyzing_report", "Applying deterministic health rules")
    previous = load_previous()
    report_id = f"daily-{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    report = {
        "report_id": report_id,
        "schema_version": SCHEMA_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "metadata": {
            "collected_at": collected_at,
            "window": window,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        },
        "sources": envelopes,
    }
    report["analysis"] = analyze(envelopes, previous=previous)
    report["report_markdown"] = render(report)
    save_snapshot(report)
    await emit_progress(
        "report_complete",
        "Deterministic daily health report complete",
        report_id=report_id,
    )
    return report
