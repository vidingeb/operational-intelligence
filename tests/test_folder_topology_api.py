"""Authoritative folder membership and actual per-VM references."""

from types import SimpleNamespace as NS

import pytest
from pyVmomi import vim

from test_vcenter_folders import (
    Folder, Managed, View, VirtualMachine, client as folder_client, inventory, v,
)
from fastapi.testclient import TestClient


class VirtualApp(Managed):
    def __init__(self, name, moid, parent, vms=(), pools=()):
        super().__init__(name, moid, parent)
        self.vm = list(vms)
        self.resourcePool = list(pools)


class Network(vim.Network):
    def __init__(self, name, moid, parent):
        super().__init__(moid)
        self._name = name
        self._parent = parent

    @property
    def name(self):
        return self._name

    @property
    def parent(self):
        return self._parent


def vm(name, moid, parent, host=None, datastores=(), devices=()):
    machine = VirtualMachine(name, moid, parent)
    machine.runtime = NS(host=host)
    machine.datastore = list(datastores)
    machine.config = NS(hardware=NS(device=list(devices)))
    return machine


def nic(key, backing):
    return vim.vm.device.VirtualVmxnet3(
        key=key, deviceInfo=vim.Description(label=f"NIC {key}", summary=""),
        backing=backing,
    )


@pytest.fixture
def setup(monkeypatch):
    root, _, folders = inventory()
    target = next(folder for folder in folders if folder._moId == "folder-target-1")
    target.childEntity = []
    monkeypatch.setattr(v.vim, "Folder", Folder)
    monkeypatch.setattr(v.vim, "VirtualMachine", VirtualMachine)
    monkeypatch.setattr(v.vim, "VirtualApp", VirtualApp)
    content = NS(rootFolder=root)
    monkeypatch.setattr(v, "get_si", lambda: NS(RetrieveContent=lambda: content))
    views = []
    groups = []

    def get_view(_content, kind):
        created = View(groups if kind is vim.dvs.DistributedVirtualPortgroup else folders)
        views.append(created)
        return created

    monkeypatch.setattr(v, "get_view", get_view)
    return NS(api=TestClient(v.app), root=root, target=target, folders=folders,
              views=views, groups=groups)


def test_selectors_duplicate_names_and_folder_ids(setup):
    response = setup.api.get("/folders/topology", params={"name": "TN3-VIDAR"})
    assert response.status_code == 409
    candidates = response.json()["detail"]["candidates"]
    assert [(row["id"], row["path"]) for row in candidates] == [
        ("folder-target-1", "/DC1/vm/Apps/tn3-vidar"),
        ("folder-target-2", "/DC2/vm/Archive/tn3-vidar"),
    ]
    for selector in ({"folder_id": candidates[0]["id"]}, {"path": candidates[0]["path"]}):
        body = setup.api.get("/folders/topology", params=selector).json()
        assert body["folder"] == candidates[0]
        assert body["schema_version"] == "folder-topology-v1"
        assert body["source"] == "vcenter"
        assert body["collected_at"].endswith("+00:00")
        assert body["recursive"] is True
        assert body["complete"] is True
        assert body["truncated"] is False
        assert body["counts"] == {"enumerated": 0, "examined": 0, "returned": 0}
        assert body["vms"] == [] and body["errors"] == []
    assert all(created.destroyed for created in setup.views)


@pytest.mark.parametrize("params", [
    {}, {"folder_id": ""}, {"name": "", "path": "/"},
    {"folder_id": "folder-target-1", "name": "tn3-vidar"},
])
def test_invalid_selectors_do_not_open_views(setup, params):
    assert setup.api.get("/folders/topology", params=params).status_code == 400
    assert setup.views == []


def test_exact_escaped_path_and_complete_absence(setup):
    setup.target.name = "Apps / % café"
    canonical = "/DC1/vm/Apps/Apps%20%2F%20%25%20caf%C3%A9"
    assert setup.api.get("/folders/topology", params={"path": canonical}).status_code == 200
    for selector in ({"path": canonical.lower()}, {"name": "Apps"}, {"folder_id": "missing"}):
        response = setup.api.get("/folders/topology", params=selector)
        # The parent folder is really named Apps: exact matching must not include the target.
        if selector == {"name": "Apps"}:
            assert response.json()["folder"]["id"] == "folder-apps"
        else:
            assert response.status_code == 404
            assert response.json()["detail"]["complete"] is True


def test_recursive_and_immediate_membership_with_vapps_and_dedup(setup):
    target = setup.target
    direct = vm("same-name", "vm-1", target)
    nested = Folder("nested", "nested", target)
    descendant = vm("same-name", "vm-2", nested)
    nested.childEntity = [descendant, direct, target]  # Includes a cycle.
    app = VirtualApp("app", "app-1", target)
    app.vm = [vm("app-vm", "vm-3", app)]
    subapp = VirtualApp("subapp", "app-2", app)
    subapp.vm = [vm("sub-vm", "vm-4", subapp)]
    app.resourcePool = [subapp]
    target.childEntity = [app, nested, direct]
    body = setup.api.get("/folders/topology", params={"folder_id": target._moId}).json()
    assert {row["id"] for row in body["vms"]} == {"vm-1", "vm-2", "vm-3", "vm-4"}
    assert body["counts"] == {"enumerated": 4, "examined": 4, "returned": 4}
    assert body["complete"] is True
    body = setup.api.get("/folders/topology", params={
        "folder_id": target._moId, "recursive": False,
    }).json()
    assert body["recursive"] is False
    assert [row["id"] for row in body["vms"]] == ["vm-1"]
    assert body["vms"][0]["host"] is None
    assert body["complete"] is True


@pytest.fixture
def reference_topology(setup):
    target = setup.target
    host = Managed("esxi", "host-1", setup.root)
    first_ds = Managed("shared", "ds-1", setup.root)
    second_ds = Managed("shared", "ds-2", setup.root)
    network = Network("actual standard network", "net-1", setup.root)
    group = Managed("actual distributed group", "pg-1", setup.root)
    group.key = "key-1"
    group.config = NS(distributedVirtualSwitch=NS(uuid="uuid-1"))
    wrong_switch = Managed("wrong switch group", "pg-2", setup.root)
    wrong_switch.key = "key-1"
    wrong_switch.config = NS(distributedVirtualSwitch=NS(uuid="uuid-2"))
    setup.groups.extend([wrong_switch, group])
    card = vim.vm.device.VirtualEthernetCard
    devices = [
        nic(3, card.OpaqueNetworkBackingInfo(opaqueNetworkId="opaque-1", opaqueNetworkType="actual-type")),
        nic(2, card.DistributedVirtualPortBackingInfo(
            port=vim.dvs.PortConnection(switchUuid="uuid-1", portgroupKey="key-1", portKey="port-1"))),
        nic(1, card.NetworkBackingInfo(network=network, deviceName="backing label")),
        nic(4, None),
    ]
    target.childEntity = [
        vm("first", "vm-1", target, host, [first_ds, second_ds, first_ds], devices),
        vm("second", "vm-2", target, datastores=[first_ds]),
    ]
    response = setup.api.get("/folders/topology", params={"folder_id": target._moId})
    assert response.status_code == 200
    return response.json()


def test_actual_host_storage_and_standard_distributed_opaque_backings(setup, reference_topology):
    body = reference_topology
    assert body["complete"] is True, body["errors"]
    first, second = body["vms"]
    assert first["host"] == {"id": "host-1", "name": "esxi", "path": "/esxi"}
    assert [row["id"] for row in first["datastores"]] == ["ds-1", "ds-2"]
    assert second["datastores"] == [first["datastores"][0]]
    assert second["host"] is None
    standard, distributed, opaque, unbacked = first["nics"]
    assert all(type(device["key"]) is int and isinstance(device["label"], str)
               for device in first["nics"])
    assert standard["network"] == {
        "id": "net-1", "name": "actual standard network",
        "path": "/actual%20standard%20network", "kind": "standard",
    }
    assert standard["backing"]["deviceName"] == "backing label"
    assert standard["backing"]["network"] == {"id": "net-1"}
    assert distributed["network"]["id"] == "pg-1"
    assert distributed["network"]["kind"] == "distributed"
    assert distributed["backing"]["port"]["portKey"] == "port-1"
    assert opaque["network"] == {
        "id": "opaque-1", "name": "opaque-1", "path": None,
        "kind": "opaque", "opaque_network_type": "actual-type",
    }
    assert unbacked["network"] is None and unbacked["backing"] == {"type": None}
    assert all(created.destroyed for created in setup.views)
    assert len(setup.views) == 2


def test_api_response_integrates_with_typed_topology_graph(reference_topology):
    from topology_diagram import build_graph

    graph = build_graph(reference_topology)
    assert graph.source.schema_version == "folder-topology-v1"
    assert graph.source.source == "vcenter"
    assert graph.source.complete is True
    assert graph.source.counts.returned == len(graph.source.vms) == 2
    assert len(graph.nodes) == 13
    assert len(graph.edges) == 13
    networks = {node.kind: node for node in graph.nodes
                if node.kind in {"standard", "distributed", "opaque"}}
    assert set(networks) == {"standard", "distributed", "opaque"}
    assert networks["standard"].object_id == "net-1"
    assert networks["distributed"].object_id == "pg-1"
    assert networks["opaque"].object_id == networks["opaque"].name == "opaque-1"
    assert networks["opaque"].path is None
    assert sum(node.kind == "datastore" for node in graph.nodes) == 2
    assert all(edge.evidence.source == "vcenter" for edge in graph.edges)


def test_unresolved_backing_retains_actual_keys_and_is_not_empty_success(setup):
    target = setup.target
    backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
        port=vim.dvs.PortConnection(switchUuid="missing-uuid", portgroupKey="missing-key"))
    target.childEntity = [vm("vm", "vm-1", target, devices=[nic(1, backing)])]
    body = setup.api.get("/folders/topology", params={"folder_id": target._moId}).json()
    assert body["complete"] is False
    device = body["vms"][0]["nics"][0]
    assert device["network"] is None
    assert device["backing"]["port"]["switchUuid"] == "missing-uuid"
    assert device["backing"]["port"]["portgroupKey"] == "missing-key"
    assert body["errors"][0]["relationship"] == "nic[1].backing"


def test_unresolved_standard_network_is_not_assumed_disconnected(setup):
    backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
        deviceName="actual unresolved label")
    setup.target.childEntity = [
        vm("vm", "vm-1", setup.target, devices=[nic(1, backing)]),
    ]
    body = setup.api.get("/folders/topology", params={"folder_id": setup.target._moId}).json()
    assert body["complete"] is False
    device = body["vms"][0]["nics"][0]
    assert device["network"] is None
    assert device["backing"]["deviceName"] == "actual unresolved label"
    assert device["backing"]["type"] is not None  # Not an explicitly absent backing.
    assert body["errors"][0]["relationship"] == "nic[1].backing"


def test_unreadable_reference_does_not_invent_name_or_drop_other_references(setup):
    unreadable_host = Managed(None, "host-unreadable", setup.root)
    unreadable_ds = Managed(None, "ds-unreadable", setup.root)
    readable_ds = Managed("known", "ds-known", setup.root)
    unreadable_network = Network("", "net-unreadable", setup.root)
    backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(network=unreadable_network)
    setup.target.childEntity = [
        vm("vm", "vm-1", setup.target, unreadable_host,
           [unreadable_ds, readable_ds], [nic(1, backing)]),
    ]
    body = setup.api.get("/folders/topology", params={"folder_id": setup.target._moId}).json()
    assert body["complete"] is False
    record = body["vms"][0]
    assert record["host"] is None
    assert record["datastores"] == [{"id": "ds-known", "name": "known", "path": "/known"}]
    assert record["nics"][0]["network"] is None
    assert record["nics"][0]["backing"]["network"] == {"id": "net-unreadable"}
    assert {item["relationship"] for item in body["errors"]} == {
        "vm.runtime.host", "vm.datastore.identity", "nic[1].backing",
    }


class BrokenFolder(Folder):
    @property
    def childEntity(self):
        raise RuntimeError("denied children")

    @childEntity.setter
    def childEntity(self, _value):
        pass


def test_failed_subtree_is_partial_not_empty_or_truncated(setup):
    target = setup.target
    target.childEntity = [vm("readable", "vm-1", target),
                          BrokenFolder("denied", "folder-denied", target)]
    body = setup.api.get("/folders/topology", params={"folder_id": target._moId}).json()
    assert body["complete"] is False and body["truncated"] is False
    assert body["counts"] == {"enumerated": 1, "examined": 1, "returned": 1}
    assert body["errors"] == [{
        "object_id": "folder-denied", "relationship": "folder.childEntity",
        "error": "RuntimeError: denied children",
    }]
    target.childEntity = [BrokenFolder("denied", "folder-denied", target)]
    body = setup.api.get("/folders/topology", params={"folder_id": target._moId}).json()
    assert body["vms"] == [] and body["complete"] is False


def test_identity_and_relationship_failures_have_truthful_counts(setup):
    target = setup.target
    unnamed = vm(None, "vm-unnamed", target)
    partial = vm("partial", "vm-partial", target)
    partial.runtime = None
    partial.datastore = None
    partial.config = None
    target.childEntity = [partial, unnamed]
    body = setup.api.get("/folders/topology", params={"folder_id": target._moId}).json()
    assert body["counts"] == {"enumerated": 2, "examined": 2, "returned": 1}
    assert body["complete"] is False
    assert body["vms"][0]["name"] == "partial"
    assert {item["relationship"] for item in body["errors"]} == {
        "vm.identity", "vm.runtime.host", "vm.datastore", "vm.config.hardware.device",
    }


def test_unreadable_nic_identity_is_omitted_without_placeholder(setup):
    unreadable = vim.vm.device.VirtualVmxnet3(key=1)
    setup.target.childEntity = [
        vm("vm", "vm-1", setup.target, devices=[unreadable, nic(2, None)]),
    ]
    body = setup.api.get("/folders/topology", params={"folder_id": setup.target._moId}).json()
    assert body["complete"] is False
    assert body["vms"][0]["nics"] == [
        {"key": 2, "label": "NIC 2", "network": None, "backing": {"type": None}},
    ]
    assert body["errors"][0]["relationship"] == "nic.identity"


def test_folder_identity_failure_never_claims_absence(setup):
    setup.folders.append(Folder(None, "unreadable", setup.root))
    response = setup.api.get("/folders/topology", params={"name": "unknown"})
    assert response.status_code == 502
    assert response.json()["detail"]["complete"] is False
    assert setup.views[0].destroyed


def test_folder_enumeration_failure_and_view_cleanup(setup, monkeypatch):
    class FailedView(View):
        @property
        def view(self):
            raise RuntimeError("enumeration denied")

        @view.setter
        def view(self, _value):
            pass

    created = FailedView([])
    monkeypatch.setattr(v, "get_view", lambda *_args: created)
    response = setup.api.get("/folders/topology", params={"name": "unknown"})
    assert response.status_code == 502
    assert response.json()["detail"]["errors"][0]["relationship"] == "folders.enumeration"
    assert created.destroyed


def test_distributed_view_creation_failure_cleans_folder_view(setup, monkeypatch):
    original = v.get_view

    def get_view(content, kind):
        if kind is vim.dvs.DistributedVirtualPortgroup:
            raise RuntimeError("view creation denied")
        return original(content, kind)

    monkeypatch.setattr(v, "get_view", get_view)
    backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
        port=vim.dvs.PortConnection(switchUuid="uuid", portgroupKey="key"))
    setup.target.childEntity = [vm("vm", "vm-1", setup.target, devices=[nic(1, backing)])]
    body = setup.api.get("/folders/topology", params={"folder_id": setup.target._moId}).json()
    assert body["complete"] is False
    assert {item["relationship"] for item in body["errors"]} == {
        "distributed_portgroups.enumeration", "nic[1].backing",
    }
    assert setup.views[0].destroyed


def test_folder_view_creation_failure_is_explicit(setup, monkeypatch):
    def fail(*_args):
        raise RuntimeError("creation denied")

    monkeypatch.setattr(v, "get_view", fail)
    response = setup.api.get("/folders/topology", params={"name": "unknown"})
    assert response.status_code == 502
    assert response.json()["detail"]["complete"] is False


def test_view_destroy_failure_marks_incomplete(setup, monkeypatch):
    created = View(setup.folders)

    def fail():
        raise RuntimeError("destroy denied")

    created.Destroy = fail
    monkeypatch.setattr(v, "get_view", lambda *_args: created)
    body = setup.api.get("/folders/topology", params={"folder_id": setup.target._moId}).json()
    assert body["complete"] is False
    assert body["errors"][0]["relationship"] == "view.destroy"


def test_existing_folder_listing_exposes_ids(monkeypatch):
    api, _, _ = folder_client(monkeypatch)
    body = api.get("/folders", params={"name": "tn3-vidar", "exact": True}).json()
    assert [row["id"] for row in body["folders"]] == ["folder-target-1", "folder-target-2"]


def test_existing_folder_listing_cleans_up_after_second_view_creation_failure(monkeypatch):
    api, _, _ = folder_client(monkeypatch)
    created = View([])

    def get_view(_content, kind):
        if kind is v.vim.Folder:
            raise RuntimeError("folder view denied")
        return created

    monkeypatch.setattr(v, "get_view", get_view)
    with pytest.raises(RuntimeError, match="folder view denied"):
        api.get("/folders")
    assert created.destroyed


class NamedProxy:
    @property
    def name(self):
        return self._name

    @property
    def parent(self):
        return self._parent


class FolderProxy(NamedProxy, vim.Folder):
    def __init__(self, name, moid, parent=None):
        super().__init__(moid)
        self._name = name
        self._parent = parent
        self._children = []

    @property
    def childEntity(self):
        return self._children


class DatacenterProxy(NamedProxy, vim.Datacenter):
    def __init__(self, parent):
        super().__init__("datacenter-3")
        self._name = "Tier0-Mgmt-dc01"
        self._parent = parent
        self._folders = {}

    vmFolder = property(lambda self: self._folders["vm"])
    hostFolder = property(lambda self: self._folders["host"])
    datastoreFolder = property(lambda self: self._folders["datastore"])
    networkFolder = property(lambda self: self._folders["network"])


class VMProxy(NamedProxy, vim.VirtualMachine):
    def __init__(self, parent):
        super().__init__("vm-101")
        self._name = "observed VM"
        self._parent = parent

    runtime = property(lambda self: NS(host=None, powerState="poweredOn"))
    datastore = property(lambda self: [])
    config = property(lambda self: NS(hardware=NS(device=[], numCPU=2, memoryMB=2048)))
    guest = property(lambda self: None)


@pytest.fixture
def proxy_inventory(monkeypatch):
    root = FolderProxy("Datacenters", "group-d1")
    parent_root = FolderProxy("Datacenters", "group-d1")
    assert root is not parent_root and root == parent_root
    dc = DatacenterProxy(parent_root)
    for category, moid in (("vm", "group-v4"), ("host", "group-h5"),
                           ("datastore", "group-s6"), ("network", "group-n7")):
        dc._folders[category] = FolderProxy(category, moid, dc)
    target = FolderProxy("Tn3-Pod1", "group-v142", dc.vmFolder)
    target._children = [VMProxy(target)]
    dc.vmFolder._children = [target]
    views = []
    content = NS(rootFolder=root)
    monkeypatch.setattr(v, "get_si", lambda: NS(RetrieveContent=lambda: content))

    def get_view(_content, kind):
        if kind is vim.Datacenter:
            values = [dc]
        elif kind is vim.VirtualMachine:
            values = target._children
        else:
            values = [*dc._folders.values(), target]
        created = View(values)
        views.append(created)
        return created

    monkeypatch.setattr(v, "get_view", get_view)
    return NS(api=TestClient(v.app), root=root, parent_root=parent_root,
              dc=dc, target=target, views=views)


def test_distinct_pyvmomi_root_proxies_resolve_system_folders_and_topology(proxy_inventory):
    inventory = proxy_inventory
    expected = "/Datacenters/Tier0-Mgmt-dc01/vm/Tn3-Pod1"
    current = inventory.target
    while current is not None and current is not inventory.root:
        current = current.parent
    assert current is None  # The old reference-identity loop misses the valid root.
    for selector in ({"folder_id": "group-v142"}, {"name": "Tn3-Pod1"}, {"path": expected}):
        response = inventory.api.get("/folders/topology", params=selector)
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["complete"] is True and body["errors"] == []
        assert body["folder"]["path"] == expected
        assert body["counts"] == {"enumerated": 1, "examined": 1, "returned": 1}
        assert body["vms"][0]["path"] == expected + "/observed%20VM"
    folders = inventory.api.get("/folders").json()
    assert folders["complete"] is True
    assert {record["id"] for record in folders["folders"] if record["is_system_folder"]} == {
        "group-v4", "group-h5", "group-s6", "group-n7",
    }
    assert next(record for record in folders["folders"]
                if record["id"] == "group-v142")["path"] == expected
    assert all(created.destroyed for created in inventory.views)


def test_paths_do_not_depend_on_root_proxy_instance(proxy_inventory):
    inventory = proxy_inventory
    proxied = v._inventory_path(inventory.target, inventory.root)
    same_instance = v._inventory_path(inventory.target, inventory.parent_root)
    assert proxied == same_instance == "/Datacenters/Tier0-Mgmt-dc01/vm/Tn3-Pod1"
    assert v._inventory_path(inventory.parent_root, inventory.root) == "/Datacenters"


def test_existing_vm_inventory_still_returns_its_original_response_shape(proxy_inventory):
    response = proxy_inventory.api.get("/vms")
    assert response.status_code == 200
    assert response.json() == [{
        "name": "observed VM", "power_state": "poweredOn", "cpu": 2, "memory_mb": 2048,
        "guest_os": None, "guest_ip": None, "vmware_tools_status": None, "host": None,
    }]
    assert proxy_inventory.views[0].destroyed


@pytest.mark.parametrize("broken", ["missing", "cycle", "cycle_distinct_proxy", "wrong_root",
                                   "wrong_type", "wrong_server", "missing_id"])
def test_real_parent_chain_failures_still_fail_closed(proxy_inventory, broken):
    inventory = proxy_inventory
    if broken == "missing":
        inventory.dc._parent = None
    elif broken == "cycle":
        inventory.dc._parent = inventory.target
    elif broken == "cycle_distinct_proxy":
        duplicate = FolderProxy("Tn3-Pod1", "group-v142", inventory.dc)
        inventory.dc._parent = duplicate
    elif broken == "wrong_root":
        inventory.dc._parent = FolderProxy("Datacenters", "group-other")
    elif broken == "wrong_type":
        impostor = DatacenterProxy(None)
        impostor._moId = inventory.root._moId
        inventory.dc._parent = impostor
    elif broken == "wrong_server":
        inventory.parent_root._serverGuid = "different-vcenter"
    else:
        inventory.dc._moId = None
    response = inventory.api.get("/folders/topology", params={"folder_id": "group-v142"})
    assert response.status_code == 502
    assert response.json()["detail"]["complete"] is False
    assert response.json()["detail"]["errors"]
    with pytest.raises(ValueError, match="Inventory"):
        v._inventory_path(inventory.target, inventory.root)
    assert all(created.destroyed for created in inventory.views)
