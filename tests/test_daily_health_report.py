"""Deterministic daily health collection, rules, rendering, and scheduling."""
import asyncio
from datetime import datetime, timezone

import health_report as h
import orchestrator as o
import store


NOW = "2026-10-01T08:00:00+00:00"
WINDOW = {
    "start": "2026-09-30T08:00:00+00:00",
    "end": "2026-10-01T08:00:00+00:00",
}


def env(source, query, data, keys=(), status="complete"):
    item = h.envelope(source, query, data, NOW, WINDOW, row_keys=keys)
    item["status"] = status
    return item


def sources(**overrides):
    base = {
        "vcenter_vms": env("vCenter", "VMs", [
            {"name": "on-good", "power_state": "poweredOn", "vmware_tools_status": "guestToolsRunning"},
            {"name": "on-bad", "power_state": "poweredOn", "vmware_tools_status": "guestToolsNotRunning"},
            {"name": "off", "power_state": "poweredOff", "vmware_tools_status": "guestToolsNotRunning"},
        ]),
        "vcenter_hosts": env("vCenter", "hosts", [
            {"name": "esx01", "connection_state": "connected"},
        ]),
        "vcenter_host_usage": env("vCenter", "usage", [
            {"name": "esx01", "cpu_total_mhz": 1000, "cpu_usage_mhz": 250,
             "cpu_usage_percent": 25, "memory_total_mb": 2000,
             "memory_usage_mb": 1000, "memory_usage_percent": 50},
        ]),
        "vcenter_datastores": env("vCenter", "datastores", [
            {"name": "vsan", "capacity_gb": 100, "used_gb": 60,
             "free_gb": 40, "used_percent": 60},
        ]),
        "vcenter_alarms": env("vCenter", "alarms", []),
        "vcenter_snapshots": env("vCenter", "snapshots", []),
        "vcf_ops_summary": env("VCF Operations", "summary", {"active_alert_count": 0}),
        "vcf_ops_alerts": env("VCF Operations", "alerts", {"alerts": []}, ("alerts",)),
        "logs_errors": env("VCF Operations for Logs", "errors", {"events": []}, ("events",)),
        "vcf_networks_flows": env(
            "VCF Operations for Networks", "flows",
            {"flows_examined": 5, "flow_count": 3, "total_flows_known": 20,
             "truncated": True, "flows": [{}, {}, {}],
             "traffic_type_breakdown": {"EAST_WEST_TRAFFIC": 5}},
            ("flows",), status="truncated",
        ),
        "vcf_networks_alerts": env(
            "VCF Operations for Networks", "alerts", {"results": []}, ("results",)
        ),
        "veeam_sessions": env("Veeam", "sessions", {
            "sessions": [
                {"name": "job-a", "result": "Success", "type": "BackupJob"},
                {"name": "job-a", "result": "Warning", "type": "BackupJob"},
                {"name": "job-b", "result": "Failed", "type": "BackupJob"},
            ]
        }, ("sessions",)),
        "veeam_protected": env("Veeam", "protected", {
            "objects": [
                {"name": "on-good", "restore_points": 2,
                 "newest_restore_point_age_hours": 4},
                {"name": "on-bad", "restore_points": 0,
                 "newest_restore_point_age_hours": None},
            ]
        }, ("objects",)),
        "veeam_jobs": env("Veeam", "jobs", {
            "jobs": [{"id": "1", "name": "job-a"}, {"id": "2", "name": "job-b"}]
        }, ("jobs",)),
    }
    base.update(h.capability_envelopes(NOW, WINDOW))
    base.update(overrides)
    return base


def test_missing_required_host_dimensions_make_host_unknown():
    analysis = h.analyze(sources())
    assert analysis["host_status"] == "UNKNOWN"
    assert analysis["host_dimensions"]["server_hardware"] == "UNKNOWN"
    assert analysis["host_dimensions"]["network_hardware"] == "UNKNOWN"


def test_storage_capacity_can_be_healthy_while_aggregate_storage_is_unknown():
    analysis = h.analyze(sources())
    assert analysis["host_dimensions"]["storage_capacity"] == "HEALTHY"
    assert analysis["host_dimensions"]["storage_latency_path"] == "UNKNOWN"
    assert analysis["host_status"] == "UNKNOWN"


def test_powered_off_tools_not_running_is_excluded_and_scores_zero():
    analysis = h.analyze(sources())
    assert analysis["counts"]["powered_on_tools_problems"] == 1
    scored = {item["object"] for item in analysis["top_10"]}
    assert "on-bad" in scored
    assert "off" not in scored


def test_backup_sessions_use_exact_results_and_jobs_are_unique():
    analysis = h.analyze(sources())
    assert analysis["counts"]["backup_sessions_by_exact_result"] == {
        "Failed": 1, "Success": 1, "Warning": 1
    }
    assert analysis["counts"]["unique_backup_jobs"] == 2
    assert analysis["backup_status"] == "WARNING"


def test_unknown_restore_point_freshness_is_not_reported_healthy():
    data = sources()
    data["veeam_sessions"] = env(
        "Veeam",
        "sessions",
        {"sessions": [{"name": "job-a", "result": "Success"}]},
        ("sessions",),
    )
    data["veeam_protected"] = env(
        "Veeam",
        "protected",
        {"objects": [{"name": "on-good", "restore_points": 2,
                      "newest_restore_point_age_hours": None}]},
        ("objects",),
    )
    analysis = h.analyze(data)
    assert analysis["backup_status"] == "UNKNOWN"
    report = {
        "report_id": "report-1",
        "collector_version": h.COLLECTOR_VERSION,
        "schema_version": h.SCHEMA_VERSION,
        "metadata": {
            "collected_at": NOW,
            "window": {"start": NOW, "end": NOW},
        },
        "sources": data,
        "analysis": analysis,
    }
    assert "| Backup | **UNKNOWN** |" in h.render(report)


def test_no_restore_point_wording_requires_review_not_automatic_enrollment():
    analysis = h.analyze(sources())
    row = next(item for item in analysis["backup"] if item["vm"] == "on-bad")
    assert row["classification"] == "no_restore_point"
    assert "review protection intent" in row["wording"].lower()
    assert "add" not in row["wording"].lower()


def test_intentional_exclusion_requires_explicit_evidence():
    excluded_vms = env("vCenter", "VMs", [
        {"name": "excluded", "power_state": "poweredOn",
         "vmware_tools_status": "guestToolsRunning", "backup_excluded": True},
        {"name": "unknown", "power_state": "poweredOn",
         "vmware_tools_status": "guestToolsRunning"},
    ])
    analysis = h.analyze(sources(vcenter_vms=excluded_vms))
    classifications = {item["vm"]: item["classification"] for item in analysis["backup"]}
    assert classifications["excluded"] == "intentionally_excluded"
    assert classifications["unknown"] == "unknown"


def test_veeam_repository_capacity_is_never_inferred_from_datastore():
    analysis = h.analyze(sources())
    assert analysis["capacity"]["datastore_total_gb"] == 100
    assert analysis["capacity"]["veeam_repository_capacity"] is None


def test_flow_semantics_keep_examined_returned_matching_and_truncated_separate():
    flow = h.analyze(sources())["flow_sampling"]
    assert flow == {
        "examined": 5,
        "returned": 3,
        "matching": 3,
        "total_known": 20,
        "truncated": True,
        "traffic_type_breakdown": {"EAST_WEST_TRAFFIC": 5},
    }


def test_capability_matrix_is_explicit():
    matrix = h.capability_envelopes(NOW, WINDOW)
    assert matrix["dell_switch_telemetry"]["status"] == "not_configured"
    assert matrix["server_hardware_health"]["status"] == "not_configured"
    assert matrix["veeam_repository_capacity"]["status"] == "unsupported"
    assert "log evidence only" in matrix["server_hardware_health"]["data"]["limitation"]


def test_collector_uses_server_clock_for_utc_window(monkeypatch, tmp_path):
    fixed = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(h, "utc_now", lambda: fixed)
    calls = []

    async def fake_call(name, args):
        calls.append(name)
        return []

    saved = []
    result = asyncio.run(h.collect(
        fake_call, lambda: None, saved.append, hours=24, flow_limit=5
    ))
    assert result["metadata"]["collected_at"] == NOW
    assert result["metadata"]["window"] == WINDOW
    assert len(calls) == 14
    assert saved[0]["report_id"] == result["report_id"]


def test_collector_reports_major_progress_phases(monkeypatch):
    async def fake_call(name, args):
        return []

    events = []

    async def progress(stage, message, details):
        events.append(stage)

    asyncio.run(h.collect(
        fake_call, lambda: None, lambda report: None, progress=progress
    ))
    assert events == [
        "collecting_vcenter",
        "collecting_operations",
        "collecting_networks",
        "collecting_backup",
        "analyzing_report",
        "report_complete",
    ]


def test_firewall_mask_event_never_correlates_with_backup_failure():
    logs = [{"host": "job-a", "time": NOW, "text": "FirewallRuleMaskEvent"}]
    sessions = [{"name": "job-a", "ended": NOW, "result": "Failed"}]
    assert h.correlate(logs, sessions) == []


def test_same_object_compatible_event_in_overlapping_time_is_possible_correlation():
    logs = [{"host": "job-a.vcf.local", "time": NOW, "text": "backup transport failed"}]
    sessions = [{"name": "job-a", "ended": NOW, "result": "Failed"}]
    result = h.correlate(logs, sessions)
    assert result[0]["label"] == "Possible correlation"
    assert result[0]["confidence"] == "medium"


def test_same_object_unrelated_event_does_not_correlate():
    logs = [{"host": "job-a", "time": NOW, "text": "user logged in"}]
    sessions = [{"name": "job-a", "ended": NOW, "result": "Failed"}]
    assert h.correlate(logs, sessions) == []


def test_score_ordering_is_rule_based_and_visible():
    alarms = env("vCenter", "alarms", [
        {"entity": "critical-vm", "overall_status": "critical", "alarm": "down"}
    ])
    analysis = h.analyze(sources(vcenter_alarms=alarms))
    assert analysis["top_10"][0]["object"] == "critical-vm"
    assert analysis["top_10"][0]["breakdown"][0]["points"] == h.SCORE_RULES["critical_alarm"]


def test_first_run_has_no_baseline_and_second_run_compares():
    first = h.analyze(sources())
    assert first["trends"]["baseline"] is False
    assert "No comparable baseline" in first["trends"]["message"]
    second = h.analyze(sources(), previous={"analysis": first})
    assert second["trends"]["baseline"] is True
    assert second["trends"]["new"] == []
    assert second["trends"]["worsened"] == []
    assert second["trends"]["improved"] == []


def test_source_attribution_and_renderer_golden_invariants():
    report = {
        "report_id": "daily-test",
        "metadata": {"collected_at": NOW, "window": WINDOW},
        "sources": sources(),
    }
    report["analysis"] = h.analyze(report["sources"])
    markdown = h.render(report)
    assert "# Daily Infrastructure Health Report" in markdown
    assert "VCF Operations" in markdown
    assert "vCenter" in markdown
    assert "Datastore/vSAN capacity is not presented as Veeam repository capacity." in markdown
    assert "Examined: **5**; returned: **3**; matching after filters: **3**" in markdown
    assert "No comparable baseline exists" in markdown
    assert "FirewallRuleMaskEvent" not in markdown


def test_snapshots_persist_and_latest_is_loaded(tmp_path):
    db = str(tmp_path / "state.db")
    report = {
        "report_id": "daily-test",
        "schema_version": 1,
        "collector_version": "v1",
        "metadata": {"collected_at": NOW},
        "analysis": {"finding_ids": []},
    }
    store.save_health_snapshot(report, path=db)
    assert store.latest_health_snapshot(path=db)["report_id"] == "daily-test"
    assert store.latest_health_snapshot(
        schema_version=2, collector_version="v2", path=db
    ) is None


def test_daily_tool_is_registered_read_only_and_dispatchable():
    spec = next(item for item in o.REGISTRY if item["name"] == "daily_health_report")
    assert spec["write"] is False
    assert o.LOCAL_HANDLERS["daily_health_report"] is o.daily_health_report
    assert "verbatim" in spec["description"]


def test_model_context_contains_report_markdown_not_raw_source_payloads():
    result = {
        "report_id": "daily-test",
        "schema_version": 1,
        "collector_version": h.COLLECTOR_VERSION,
        "sources": {"secret-sized-payload": [{"x": 1}]},
        "report_markdown": "# deterministic",
    }
    shaped = o.summarize_tool_result(result)
    assert "# deterministic" in shaped
    assert "secret-sized-payload" not in shaped
    assert "Reproduce report_markdown verbatim" in shaped


def test_scheduled_daily_report_bypasses_model_and_stays_read_only(monkeypatch, tmp_path):
    db = str(tmp_path / "state.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    called = []

    async def fake_report(**kwargs):
        called.append(True)
        return {
            "report_id": "daily-test",
            "schema_version": 1,
            "report_markdown": "# deterministic",
        }

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("scheduled deterministic report reached the model")

    monkeypatch.setattr(o, "daily_health_report", fake_report)
    monkeypatch.setattr(o, "chat_with_tools", forbidden_model)
    schedule = {
        "id": "sched", "question": "daily health report", "model": None, "scope": "all"
    }
    run_id = asyncio.run(o.run_scheduled(schedule))
    run = store.get_run(run_id, path=db)
    assert called == [True]
    assert run["answer"] == "# deterministic"
    assert run["tools_called"] == ["daily_health_report"]
