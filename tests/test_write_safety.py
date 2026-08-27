"""Write safety and triage.

The write tools can hard-power-off production VMs and delete every snapshot,
so the guarantee under test is narrow and absolute: calling a write tool must
not reach the API until someone confirms it by token.
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))
os.environ["ENABLE_WRITE_TOOLS"] = "true"
os.environ["WRITE_REQUIRE_CONFIRM"] = "true"
_AUDIT = os.path.join(tempfile.mkdtemp(), "audit.log")
os.environ["AUDIT_LOG"] = _AUDIT

import orchestrator as o  # noqa: E402

o.AUDIT_LOG = _AUDIT


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class FakeClient:
    """Records every HTTP call the orchestrator makes."""

    calls = []
    vm_state = {"name": "adc01", "power_state": "poweredOn", "host": "esx04.vcf.local"}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        FakeClient.calls.append(("GET", url, params))
        return FakeResponse(dict(FakeClient.vm_state))

    async def post(self, url, json=None):
        FakeClient.calls.append(("POST", url, json))
        # A real power off changes the state the verification step re-reads.
        if url.endswith("/vm/poweroff"):
            FakeClient.vm_state["power_state"] = "poweredOff"
        return FakeResponse({"task": "success"})


def setup_function(_):
    FakeClient.calls = []
    FakeClient.vm_state = {"name": "adc01", "power_state": "poweredOn", "host": "esx04.vcf.local"}
    o.PENDING.clear()
    o.httpx.AsyncClient = FakeClient
    open(_AUDIT, "w").close()


def run(coro):
    return asyncio.run(coro)


def writes_attempted():
    return [c for c in FakeClient.calls if c[0] == "POST"]


# --- The core guarantee ------------------------------------------------------

def test_write_tool_does_not_reach_the_api():
    result = run(o.call_api("vcenter_vm_poweroff", {"name": "adc01"}))
    assert result["status"] == "AWAITING_CONFIRMATION"
    assert result["executed"] is False
    assert writes_attempted() == [], "a write reached the API without confirmation"


def test_proposal_reads_current_state_for_blast_radius():
    result = run(o.call_api("vcenter_vm_poweroff", {"name": "adc01"}))
    assert result["current_state"]["power_state"] == "poweredOn"
    assert ("GET", f"{o.VCENTER_BASE}/vm/details", {"name": "adc01"}) in FakeClient.calls


def test_irreversible_operations_are_flagged_with_a_reason():
    result = run(o.call_api("vcenter_vm_snapshot_remove_all", {"name": "adc01"}))
    assert result["irreversible"] is True
    assert "no undo" in result["warning"].lower()


def test_reversible_operation_is_not_flagged_irreversible():
    result = run(o.call_api("vcenter_vm_poweron", {"name": "adc01"}))
    assert result["irreversible"] is False
    assert result["warning"] is None


def test_proposal_tells_the_model_not_to_claim_success():
    result = run(o.call_api("vcenter_vm_vmotion", {"name": "adc01", "target_host": "esx01"}))
    assert "NOTHING HAS BEEN CHANGED" in result["instruction"]


# --- Confirmation ------------------------------------------------------------

def test_confirmation_executes_and_verifies_the_new_state():
    proposal = run(o.call_api("vcenter_vm_poweroff", {"name": "adc01"}))
    outcome = run(o.execute_pending(proposal["confirmation_token"]))

    assert outcome["status"] == "EXECUTED"
    assert outcome["executed"] is True
    assert len(writes_attempted()) == 1
    # State is re-read afterwards rather than trusting the API's own success.
    assert outcome["state_before"]["power_state"] == "poweredOn"
    assert outcome["state_after"]["power_state"] == "poweredOff"


def test_token_is_single_use():
    proposal = run(o.call_api("vcenter_vm_poweron", {"name": "adc01"}))
    token = proposal["confirmation_token"]
    run(o.execute_pending(token))
    try:
        run(o.execute_pending(token))
        raise AssertionError("a token was replayable")
    except o.HTTPException as exc:
        assert exc.status_code == 404


def test_unknown_token_is_rejected():
    try:
        run(o.execute_pending("not-a-real-token"))
        raise AssertionError("an invented token was accepted")
    except o.HTTPException as exc:
        assert exc.status_code == 404
    assert writes_attempted() == []


def test_expired_token_is_rejected():
    proposal = run(o.call_api("vcenter_vm_poweroff", {"name": "adc01"}))
    token = proposal["confirmation_token"]
    o.PENDING[token]["proposed_at"] -= (o.PENDING_TTL + 60)
    try:
        run(o.execute_pending(token))
        raise AssertionError("an expired token was accepted")
    except o.HTTPException as exc:
        assert exc.status_code == 404
    assert writes_attempted() == []


# --- Audit -------------------------------------------------------------------

def test_every_proposal_and_execution_is_audited():
    proposal = run(o.call_api("vcenter_vm_poweroff", {"name": "adc01"}))
    run(o.execute_pending(proposal["confirmation_token"]))

    events = [json.loads(line) for line in open(_AUDIT) if line.strip()]
    assert [e["event"] for e in events] == ["proposed", "executed"]
    assert all(e["tool"] == "vcenter_vm_poweroff" for e in events)
    assert events[1]["state_after"]["power_state"] == "poweredOff"


def test_read_tools_are_never_audited_or_gated():
    result = run(o.call_api("vcenter_vm_details", {"name": "adc01"}))
    assert "confirmation_token" not in result
    assert os.path.getsize(_AUDIT) == 0


# --- Triage ------------------------------------------------------------------

def test_triage_filters_alarms_to_the_named_object():
    rows = [{"entity": "adc01", "alarm": "cpu"}, {"entity": "web02", "alarm": "mem"}]
    assert o._filter_mentions({"alarms": rows}, "adc01") == [rows[0]]


def test_triage_vm_consults_all_three_systems():
    async def fake_call(tool, args=None, confirmed=False):
        return {"tool": tool, "args": args}

    real, o.call_api = o.call_api, fake_call
    try:
        out = run(o.triage_vm("adc01"))
    finally:
        o.call_api = real

    assert out["triage_target"] == "adc01"
    assert set(out["systems_consulted"]) == {"vCenter", "VCF Operations", "VCF Networks"}
    for section in ("vm", "snapshots", "storage", "vcenter_alarms", "ops_alerts", "recent_flows"):
        assert section in out


def test_triage_reports_a_failed_section_instead_of_aborting():
    async def fake_call(tool, args=None, confirmed=False):
        if tool == "ops_critical_alerts":
            raise RuntimeError("VCF Operations unreachable")
        return {"ok": tool}

    real, o.call_api = o.call_api, fake_call
    try:
        out = run(o.triage_estate())
    finally:
        o.call_api = real

    assert out["sections_failed"] == ["ops_critical_alerts"]
    assert "unreachable" in out["ops_critical_alerts"]["error"]
    # The other checks still ran.
    assert out["vcenter_alarms"] == {"ok": "vcenter_alarms"}
