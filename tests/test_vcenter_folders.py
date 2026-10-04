"""Authoritative vCenter inventory folder listing and search."""

import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vcenter"))

import vcenter_api as v  # noqa: E402
import orchestrator as o  # noqa: E402


class Managed:
    def __init__(self, name, moid, parent=None):
        self.name = name
        self._moId = moid
        self.parent = parent


class Folder(Managed):
    def __init__(self, name, moid, parent=None, children=None):
        super().__init__(name, moid, parent)
        self.childEntity = children or []


class VirtualMachine(Managed):
    pass


class HostSystem(Managed):
    pass


class ClusterComputeResource(Managed):
    pass


class ComputeResource(Managed):
    pass


class Network(Managed):
    pass


class Datastore(Managed):
    pass


class Datacenter(Managed):
    pass


class View:
    def __init__(self, values):
        self.view = values
        self.destroyed = False

    def Destroy(self):
        self.destroyed = True


def inventory():
    root = Folder("", "root")
    dc1 = Datacenter("DC1", "dc-1", root)
    dc2 = Datacenter("DC2", "dc-2", root)
    vm1 = Folder("vm", "group-vm-1", dc1)
    vm2 = Folder("vm", "group-vm-2", dc2)
    apps = Folder("Apps", "folder-apps", vm1)
    archive = Folder("Archive", "folder-archive", vm2)
    first = Folder("tn3-vidar", "folder-target-1", apps)
    second = Folder("tn3-vidar", "folder-target-2", archive)
    first.childEntity = [
        VirtualMachine("vm-b", "vm-2", first),
        VirtualMachine("vm-a", "vm-1", first),
    ]
    apps.childEntity = [first]
    archive.childEntity = [second]
    vm1.childEntity = [apps]
    vm2.childEntity = [archive]
    dc1.vmFolder = vm1
    dc2.vmFolder = vm2
    for dc in (dc1, dc2):
        dc.hostFolder = Folder("host", f"{dc._moId}-host", dc)
        dc.networkFolder = Folder("network", f"{dc._moId}-network", dc)
        dc.datastoreFolder = Folder("datastore", f"{dc._moId}-datastore", dc)
    folders = [second, apps, vm2, first, archive, vm1]
    return root, [dc1, dc2], folders


def client(monkeypatch):
    root, datacenters, folders = inventory()
    folder_views = []
    datacenter_views = []

    for name, cls in (
        ("Folder", Folder),
        ("VirtualMachine", VirtualMachine),
        ("HostSystem", HostSystem),
        ("ClusterComputeResource", ClusterComputeResource),
        ("ComputeResource", ComputeResource),
        ("Network", Network),
        ("Datastore", Datastore),
        ("Datacenter", Datacenter),
    ):
        monkeypatch.setattr(v.vim, name, cls)

    content = type("Content", (), {"rootFolder": root})()
    service = type("Service", (), {"RetrieveContent": lambda self: content})()
    monkeypatch.setattr(v, "get_si", lambda: service)

    def get_view(_content, kind):
        view = View(datacenters if kind is Datacenter else folders)
        (datacenter_views if kind is Datacenter else folder_views).append(view)
        return view

    monkeypatch.setattr(v, "get_view", get_view)
    return TestClient(v.app), folder_views, datacenter_views


def test_nested_duplicate_names_keep_distinct_full_paths(monkeypatch):
    api, folder_views, datacenter_views = client(monkeypatch)
    response = api.get("/folders", params={"name": "TN3-VIDAR", "exact": True})
    assert response.status_code == 200
    body = response.json()
    assert [item["path"] for item in body["folders"]] == [
        "/DC1/vm/Apps/tn3-vidar",
        "/DC2/vm/Archive/tn3-vidar",
    ]
    assert all(item["category"] == "vm" for item in body["folders"])
    assert body["match_semantics"] == "case_insensitive_exact"
    assert body["complete"] is True
    assert folder_views[-1].destroyed and datacenter_views[-1].destroyed


def test_contains_search_is_case_insensitive_and_deterministic(monkeypatch):
    api, _, _ = client(monkeypatch)
    body = api.get("/folders", params={"name": "CHIV"}).json()
    paths = [item["path"] for item in body["folders"]]
    assert paths == sorted(paths, key=lambda path: (path.casefold(), path))
    assert paths == ["/DC2/vm/Archive"]
    assert body["match_semantics"] == "case_insensitive_contains"


def test_zero_result_is_explicitly_complete(monkeypatch):
    api, _, _ = client(monkeypatch)
    body = api.get("/folders", params={"name": "does-not-exist"}).json()
    assert body["folders"] == []
    assert body["count"] == 0
    assert body["complete"] is True


def test_system_folders_are_labelled_and_child_summary_is_bounded(monkeypatch):
    api, _, _ = client(monkeypatch)
    body = api.get("/folders").json()
    system = next(item for item in body["folders"] if item["path"] == "/DC1/vm")
    target = next(
        item for item in body["folders"]
        if item["path"] == "/DC1/vm/Apps/tn3-vidar"
    )
    assert system["is_system_folder"] is True
    assert system["category"] == "vm"
    assert target["immediate_children"]["count"] == 2
    assert target["immediate_children"]["counts_by_type"] == {"virtual_machine": 2}
    assert [item["name"] for item in target["immediate_children"]["sample"]] == [
        "vm-a",
        "vm-b",
    ]


def test_authoritative_folder_tool_is_registered_in_vcenter_scope():
    spec = o.TOOL_SPECS["vcenter_folders"]
    assert spec["method"] == "GET"
    assert spec["url"].endswith(":8080/folders")
    assert "authoritative" in spec["description"].lower()
    assert {"name", "exact"} <= set(spec["params"])
    scoped = {
        tool["function"]["name"] for tool in o.TOOLS_BY_SCOPE["vcenter"]
    }
    assert "vcenter_folders" in scoped


def test_prompt_requires_complete_folder_search_before_absence_claim():
    prompt = o.prompt_for("vcenter")
    assert "vcenter_folders is authoritative" in prompt
    assert "complete=true with zero matches" in prompt
    assert "Never describe an invented tool" in prompt
