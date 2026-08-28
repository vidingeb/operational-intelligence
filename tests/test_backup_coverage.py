"""Backup coverage: the join Veeam cannot do on its own.

The live estate has 63 VMs in vCenter and 11 objects in Veeam, and Veeam
reported "0 without restore points" — a true statement about its own roster
that reads as an all-clear for the estate. These tests pin the difference.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "veeam"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))
os.environ.setdefault("VEEAM_URL", "https://fake:9419")
os.environ.setdefault("VEEAM_USER", "u")
os.environ.setdefault("VEEAM_PASSWORD", "p")

import veeam_api as v  # noqa: E402
import orchestrator as o  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def stub_gather(monkeypatch, vms, veeam):
    async def fake(sections):
        out = {}
        for name, (tool, _args) in sections.items():
            out[name] = vms if tool == "vcenter_list_vms" else veeam
        return out
    monkeypatch.setattr(o, "_gather", fake)


def veeam_payload(objects, complete=True):
    return {
        "objects_known_to_veeam": len(objects),
        "complete": complete,
        "objects": objects,
    }


def obj(name, points=3, age=2.0):
    return {"name": name, "restore_points": points,
            "newest_restore_point": "2026-08-27T22:00:00Z",
            "newest_restore_point_age_hours": age}


# --- the join ------------------------------------------------------------

def test_a_vm_absent_from_veeam_counts_as_unprotected(monkeypatch):
    """The whole point: Veeam's own report cannot see this VM at all."""
    vms = [{"name": "vc01"}, {"name": "forgotten-vm"}]
    stub_gather(monkeypatch, vms, veeam_payload([obj("vc01")]))

    result = run(o.backup_coverage())

    assert result["vms_in_vcenter"] == 2
    assert result["vms_without_a_restore_point"] == 1
    assert [r["vm"] for r in result["unprotected"]] == ["forgotten-vm"]
    assert result["unprotected"][0]["reason"] == "not present in Veeam at all"


def test_the_two_reasons_are_distinguished(monkeypatch):
    """Never protected and a failing job need different responses."""
    vms = [{"name": "never-added"}, {"name": "job-failing"}]
    stub_gather(monkeypatch, vms, veeam_payload([obj("job-failing", points=0)]))

    reasons = {r["vm"]: r["reason"] for r in run(o.backup_coverage())["unprotected"]}

    assert reasons["never-added"] == "not present in Veeam at all"
    assert reasons["job-failing"] == "known to Veeam but has no restore point"


def test_names_match_across_a_dns_suffix_and_case(monkeypatch):
    """vCenter shows sddc01.bervid.local where Veeam records SDDC01."""
    vms = [{"name": "sddc01.bervid.local"}]
    stub_gather(monkeypatch, vms, veeam_payload([obj("SDDC01")]))

    result = run(o.backup_coverage())

    assert result["vms_without_a_restore_point"] == 0
    assert result["vms_with_a_restore_point"] == 1


def test_an_empty_veeam_roster_makes_every_vm_unprotected(monkeypatch):
    """If Veeam protects nothing, the answer is not "nothing is wrong"."""
    vms = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    stub_gather(monkeypatch, vms, veeam_payload([]))

    result = run(o.backup_coverage())

    assert result["vms_without_a_restore_point"] == 3


def test_a_stale_restore_point_is_reported_but_not_counted_as_missing(monkeypatch):
    vms = [{"name": "old"}]
    stub_gather(monkeypatch, vms, veeam_payload([obj("old", age=500.0)]))

    result = run(o.backup_coverage())

    assert result["vms_without_a_restore_point"] == 0
    assert result["vms_with_a_stale_restore_point"] == 1
    assert result["stale"][0]["age_hours"] == 500.0


def test_a_failed_section_reports_unknown_rather_than_covered(monkeypatch):
    """An unavailable check must never read as a passed check."""
    stub_gather(monkeypatch, [{"name": "a"}], {"error": "connection refused"})

    result = run(o.backup_coverage())

    assert result["sections_failed"] == ["veeam"]
    assert "unknown" in result["guidance"]
    assert "vms_without_a_restore_point" not in result


def test_guidance_ranks_coverage_above_licences_and_alarms(monkeypatch):
    """The model previously ranked an expired licence above failing backups."""
    stub_gather(monkeypatch, [{"name": "a"}], veeam_payload([]))

    guidance = run(o.backup_coverage())["guidance"]

    assert "outranks" in guidance
    assert "licence" in guidance


def test_an_incomplete_veeam_roster_is_surfaced(monkeypatch):
    """A partial roster overstates the gap; the caller has to be told."""
    stub_gather(monkeypatch, [{"name": "a"}], veeam_payload([], complete=False))

    result = run(o.backup_coverage())

    assert result["veeam_roster_complete"] is False
    assert "veeam_roster_complete" in result["guidance"]


def test_the_tool_is_registered_and_dispatchable():
    entry = next(t for t in o.REGISTRY if t["name"] == "backup_coverage")
    assert entry["description"]
    assert o.LOCAL_HANDLERS["backup_coverage"] is o.backup_coverage


# --- pagination ----------------------------------------------------------

def test_every_page_is_fetched(monkeypatch):
    """Stopping at page one is how a coverage check becomes a sample."""
    rows = [{"name": f"vm{i}", "restorePointsCount": 1} for i in range(450)]
    seen = []

    def fake(method, path, params=None, **kw):
        seen.append(params["skip"])
        skip, limit = params["skip"], params["limit"]
        return {"data": rows[skip:skip + limit],
                "pagination": {"total": len(rows), "skip": skip}}

    monkeypatch.setattr(v, "request", fake)
    fetched, total = v._all_pages("/api/v1/backupObjects")

    assert len(fetched) == 450
    assert total == 450
    assert seen == [0, 200, 400]


def test_a_full_final_page_does_not_loop_forever(monkeypatch):
    """total is the stop condition; a page-size-aligned set must terminate."""
    rows = [{"name": f"vm{i}"} for i in range(400)]

    def fake(method, path, params=None, **kw):
        skip, limit = params["skip"], params["limit"]
        return {"data": rows[skip:skip + limit], "pagination": {"total": 400}}

    monkeypatch.setattr(v, "request", fake)
    fetched, _ = v._all_pages("/api/v1/backupObjects")

    assert len(fetched) == 400


def test_protected_keeps_objects_with_zero_restore_points(monkeypatch):
    """/veeam/unprotected discards these; the roster needs all of them."""
    def fake(method, path, params=None, **kw):
        if params["skip"]:
            return {"data": [], "pagination": {"total": 2}}
        return {"data": [
            {"name": "backed-up", "restorePointsCount": 7,
             "lastRestorePointDate": "2026-08-27T22:00:00Z"},
            {"name": "empty", "restorePointsCount": 0},
        ], "pagination": {"total": 2}}

    monkeypatch.setattr(v, "request", fake)
    result = v.protected()

    assert result["objects_known_to_veeam"] == 2
    assert result["with_restore_points"] == 1
    assert result["without_restore_points"] == 1
    assert result["complete"] is True


def test_a_short_roster_against_a_larger_total_is_marked_incomplete(monkeypatch):
    """Silently returning half the roster would invent unprotected VMs."""
    def fake(method, path, params=None, **kw):
        return {"data": [{"name": "a", "restorePointsCount": 1}] if not params["skip"] else [],
                "pagination": {"total": 99}}

    monkeypatch.setattr(v, "request", fake)
    result = v.protected()

    assert result["reported_total"] == 99
    assert result["complete"] is False
