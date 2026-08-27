"""VM hardware and VMware Tools version reporting.

Built from the live payload of the 63-VM estate. Two values in that response
are traps rather than facts:

  toolsVersion 2147483647 is int32 max, meaning "not reported" — passed
  through, it produces an answer claiming a tools version of two billion.

  guestToolsUnmanaged was the largest bucket at 39 VMs. It means open-vm-tools
  managed by the guest OS, which is correct for Photon appliances. Reported
  without that context it reads as 39 broken VMs.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vcenter"))


class _FakeGuest:
    def __init__(self, tools_version=None, status="guestToolsCurrent",
                 running="guestToolsRunning", guest_os="VMware Photon OS (64-bit)"):
        self.toolsVersion = tools_version
        self.toolsVersionStatus2 = status
        self.toolsRunningStatus = running
        self.guestFullName = guest_os


class _FakeVM:
    def __init__(self, name, hw, template=False, power="poweredOn", guest=None,
                 config=True):
        self.name = name
        self.runtime = types.SimpleNamespace(powerState=power)
        self.guest = guest or _FakeGuest()
        if config:
            self.config = types.SimpleNamespace(
                version=hw, template=template,
                guestFullName="VMware Photon OS (64-bit)")
        else:
            self.config = None


def _load(vms, monkeypatch):
    import vcenter_api as m

    class _View:
        view = vms

        def Destroy(self):
            pass

    monkeypatch.setattr(m, "get_si", lambda: types.SimpleNamespace(
        RetrieveContent=lambda: object()))
    monkeypatch.setattr(m, "get_view", lambda content, kind: _View())
    return m


# vm_versions is a FastAPI endpoint: called directly, its default limit is an
# unresolved Query object, so every call here passes limit explicitly.


def test_sentinel_tools_version_becomes_null(monkeypatch):
    """2147483647 is int32 max, not a version."""
    vms = [_FakeVM("tmpl", "vmx-15", template=True, power="poweredOff",
                   guest=_FakeGuest(tools_version="2147483647",
                                    status="guestToolsUnmanaged",
                                    running="guestToolsNotRunning"))]
    m = _load(vms, monkeypatch)

    row = m.vm_versions(limit=500)["vms"][0]
    assert row["tools_version"] is None


def test_real_tools_version_is_preserved(monkeypatch):
    vms = [_FakeVM("MCP-LLM", "vmx-22", guest=_FakeGuest(tools_version="12352"))]
    m = _load(vms, monkeypatch)

    assert m.vm_versions(limit=500)["vms"][0]["tools_version"] == "12352"


def test_hardware_versions_sorted_newest_first(monkeypatch):
    vms = [_FakeVM("a", "vmx-10"), _FakeVM("b", "vmx-22"), _FakeVM("c", "vmx-9")]
    m = _load(vms, monkeypatch)

    hw = m.vm_versions(limit=500)["hardware_versions"]
    assert hw["newest_in_use"] == "vmx-22"
    assert [b["hardware_version"] for b in hw["by_version"]] == \
        ["vmx-22", "vmx-10", "vmx-9"], "must sort numerically, not as strings"
    assert hw["not_on_newest"] == 2


def test_powered_on_without_tools_is_isolated(monkeypatch):
    """Ties directly to backup quality — no tools means no quiescing."""
    vms = [
        _FakeVM("live-no-tools", "vmx-20",
                guest=_FakeGuest(status="guestToolsNotInstalled",
                                 running="guestToolsNotRunning")),
        _FakeVM("off-no-tools", "vmx-20", power="poweredOff",
                guest=_FakeGuest(status="guestToolsNotInstalled",
                                 running="guestToolsNotRunning")),
        _FakeVM("tmpl-no-tools", "vmx-20", template=True,
                guest=_FakeGuest(status="guestToolsNotInstalled",
                                 running="guestToolsNotRunning")),
    ]
    m = _load(vms, monkeypatch)

    out = m.vm_versions(limit=500)["powered_on_without_tools"]
    assert out["count"] == 1
    assert out["vms"] == ["live-no-tools"], \
        "powered-off VMs and templates without tools are not a finding"


def test_unmanaged_status_is_explained(monkeypatch):
    """39 of 63 VMs were unmanaged; without context that reads as 39 faults."""
    vms = [_FakeVM("a", "vmx-22", guest=_FakeGuest(status="guestToolsUnmanaged"))]
    m = _load(vms, monkeypatch)

    out = m.vm_versions(limit=500)
    assert out["tools_version_status"]["guestToolsUnmanaged"] == 1
    assert "NOT a fault" in out["status_meanings"]["guestToolsUnmanaged"]


def test_templates_counted_separately(monkeypatch):
    vms = [_FakeVM("t", "vmx-21", template=True), _FakeVM("v", "vmx-21")]
    m = _load(vms, monkeypatch)

    bucket = m.vm_versions(limit=500)["hardware_versions"]["by_version"][0]
    assert bucket["count"] == 2
    assert bucket["templates"] == 1


def test_unreadable_vm_is_reported_not_skipped(monkeypatch):
    vms = [_FakeVM("ok", "vmx-21"), _FakeVM("orphan", None, config=False)]
    m = _load(vms, monkeypatch)

    out = m.vm_versions(limit=500)
    assert out["inaccessible_vms"] == ["orphan"]
    assert out["vm_count"] == 1


def test_limit_truncates_rows_but_not_counts(monkeypatch):
    vms = [_FakeVM(f"vm{i}", "vmx-21") for i in range(10)]
    m = _load(vms, monkeypatch)

    out = m.vm_versions(limit=3)
    assert len(out["vms"]) == 3
    assert out["vms_truncated"] is True
    assert out["vm_count"] == 10, "the count must cover every VM, not just returned rows"


def test_summary_states_the_on_newest_count_as_a_sentence(monkeypatch):
    """Live run: 11 of 63 on vmx-22, and the model still said "most VMs are
    already on the newest hardware version". The counts were in the payload
    and were not used, so the conclusion is now stated outright."""
    vms = ([_FakeVM(f"new{i}", "vmx-22") for i in range(11)] +
           [_FakeVM(f"old{i}", "vmx-13") for i in range(52)])
    m = _load(vms, monkeypatch)

    hw = m.vm_versions(limit=1)["hardware_versions"]
    assert hw["on_newest"] == 11
    assert hw["not_on_newest"] == 52
    assert hw["summary"] == "11 of 63 VMs are on vmx-22; 52 are on older hardware versions."


def test_all_on_newest_summary(monkeypatch):
    vms = [_FakeVM("a", "vmx-22"), _FakeVM("b", "vmx-22")]
    m = _load(vms, monkeypatch)

    hw = m.vm_versions(limit=1)["hardware_versions"]
    assert hw["on_newest"] == 2 and hw["not_on_newest"] == 0
    assert "2 of 2" in hw["summary"]


def test_no_readable_vms_makes_no_claim(monkeypatch):
    m = _load([], monkeypatch)

    out = m.vm_versions(limit=1)
    assert out["vm_count"] == 0
    assert out["hardware_versions"]["summary"] == "No VM hardware versions were readable."
