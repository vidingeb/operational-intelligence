"""Measured inventory graphs. Neither graph contents nor reports come from an LLM."""

import html
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "folder-topology-v1"


class FolderSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    folder_id: str | None = None
    path: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        values = [value for value in (self.folder_id, self.path, self.name)
                  if value is not None]
        if len(values) != 1 or not values[0].strip():
            raise ValueError("Supply exactly one non-empty folder_id, full path, or exact name.")
        if self.path is not None and not self.path.startswith("/"):
            raise ValueError("Folder path must be a full inventory path starting with '/'.")
        return self


class Identity(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    path: str


class Network(Identity):
    path: str | None
    kind: Literal["standard", "distributed", "opaque"]


class NIC(BaseModel):
    key: int
    label: str
    network: Network | None
    backing: dict


class VM(Identity):
    host: Identity | None
    datastores: list[Identity]
    nics: list[NIC]


class CollectionError(BaseModel):
    object_id: str | None
    relationship: str
    error: str


class Counts(BaseModel):
    enumerated: int = Field(ge=0)
    examined: int = Field(ge=0)
    returned: int = Field(ge=0)


class Topology(BaseModel):
    schema_version: Literal["folder-topology-v1"]
    source: Literal["vcenter"]
    collected_at: str
    folder: Identity
    recursive: bool
    complete: bool
    errors: list[CollectionError]
    counts: Counts
    truncated: bool
    vms: list[VM]

    @field_validator("collected_at")
    @classmethod
    def utc_collection_time(cls, value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("Topology collection timestamp must include UTC timezone.")
        return value

    @model_validator(mode="after")
    def consistent_counts(self):
        if (self.counts.returned != len(self.vms)
                or len({vm.id for vm in self.vms}) != len(self.vms)
                or not self.counts.returned <= self.counts.examined <= self.counts.enumerated):
            raise ValueError("Inconsistent VM counts or duplicate VM IDs from topology API.")
        if self.complete and (self.errors or self.truncated
                              or self.counts.returned != self.counts.enumerated):
            raise ValueError("Topology API claimed completeness despite missing objects/errors.")
        return self


class Node(BaseModel):
    id: str
    kind: Literal["folder", "vm", "host", "datastore", "nic", "standard", "distributed", "opaque"]
    object_id: str
    name: str
    path: str | None


class Evidence(BaseModel):
    source: Literal["vcenter"]
    endpoint: Literal["/folders/topology"]
    object_id: str
    relationship: str
    target_id: str


class Edge(BaseModel):
    source: str
    target: str
    relationship: Literal["member", "runtime host", "datastore backing", "vNIC", "network backing"]
    evidence: Evidence


class Graph(BaseModel):
    schema_version: Literal["measured-graph-v1"] = "measured-graph-v1"
    nodes: list[Node]
    edges: list[Edge]
    source: Topology


def node_id(kind: str, object_id: str) -> str:
    # Hex of the full identity is injective, unlike a shortened digest or a name.
    return "n" + f"{kind}\0{object_id}".encode("utf-8").hex()


def build_graph(data: dict) -> Graph:
    topology = Topology.model_validate(data)
    nodes: dict[str, Node] = {}
    edges: dict[tuple[str, str, str], Edge] = {}

    def add(kind: str, obj: Identity) -> str:
        key = node_id(kind, obj.id)
        node = Node(id=key, kind=kind, object_id=obj.id, name=obj.name, path=obj.path)
        if key in nodes and nodes[key] != node:
            raise ValueError(f"Conflicting identities for {kind} object {obj.id}.")
        nodes[key] = node
        return key

    def link(start, end, label, obj, prop, target):
        edges[start, end, prop] = Edge(
            source=start, target=end, relationship=label,
            evidence=Evidence(source="vcenter", endpoint="/folders/topology",
                              object_id=obj, relationship=prop, target_id=target),
        )

    folder = add("folder", topology.folder)
    for vm in topology.vms:
        machine = add("vm", vm)
        link(folder, machine, "member", topology.folder.id,
             "childEntity recursive traversal" if topology.recursive else "childEntity", vm.id)
        if vm.host is not None:
            host = add("host", vm.host)
            link(machine, host, "runtime host", vm.id, "runtime.host", vm.host.id)
        for ds in vm.datastores:
            datastore = add("datastore", ds)
            link(machine, datastore, "datastore backing", vm.id, "datastore", ds.id)
        if len({nic.key for nic in vm.nics}) != len(vm.nics):
            raise ValueError(f"Duplicate vNIC keys on VM {vm.id}.")
        for nic in vm.nics:
            nic_identity = Identity(id=f"{len(vm.id)}:{vm.id}:{nic.key}",
                                    name=nic.label or str(nic.key), path=vm.path)
            adapter = add("nic", nic_identity)
            prop = f"config.hardware.device[key={nic.key}]"
            link(machine, adapter, "vNIC", vm.id, prop, str(nic.key))
            if nic.network is not None:
                network = add(nic.network.kind, nic.network)
                link(adapter, network, "network backing", vm.id,
                     f"{prop}.backing", nic.network.id)
    topology.vms.sort(key=lambda vm: vm.id)
    for vm in topology.vms:
        vm.datastores = sorted({ds.id: ds for ds in vm.datastores}.values(), key=lambda ds: ds.id)
        vm.nics.sort(key=lambda nic: nic.key)
    topology.errors.sort(key=lambda error: (
        error.object_id or "", error.relationship, error.error))
    return Graph(nodes=sorted(nodes.values(), key=lambda node: node.id),
                 edges=sorted(edges.values(), key=lambda edge: (
                     edge.source, edge.target, edge.evidence.relationship)),
                 source=topology)


def mermaid_label(text: str) -> str:
    # Mermaid numeric entities prevent quotes, directives, HTML and newlines
    # from becoming diagram syntax. Labels still display their original text.
    return "".join(char if char.isascii() and (char.isalnum() or char == " ")
                   else f"#{ord(char)};" for char in text)


def mermaid(graph: Graph) -> str:
    lines = ["flowchart LR"]
    for node in graph.nodes:
        lines.append(f'    {node.id}["{mermaid_label(node.kind + ": " + node.name)}"]')
    for edge in graph.edges:
        lines.append(f'    {edge.source} -->|"{mermaid_label(edge.relationship)}"| {edge.target}')
    return "\n".join(lines)


def markdown_text(text: str) -> str:
    escaped = html.escape(text, quote=True)
    for char in ("|", "`", "\r", "\n", "*", "_", "[", "]", "\\"):
        escaped = escaped.replace(char, f"&#{ord(char)};")
    return escaped


def report(graph: Graph, source: str, image_url: str | None, render_error: str | None) -> str:
    data = graph.source
    counts = data.counts
    lines = [
        "## Measured infrastructure sketch",
        "",
        f"Folder: **{markdown_text(data.folder.name)}** "
        f"({markdown_text(data.folder.id)}), path: {markdown_text(data.folder.path)}.",
        f"Source: vCenter {markdown_text(source)}; collected UTC: {markdown_text(data.collected_at)}.",
        f"Membership: {'recursive' if data.recursive else 'immediate'}.",
        f"Collection: **{'complete' if data.complete else 'INCOMPLETE'}**; "
        f"VMs enumerated: {counts.enumerated}; examined: {counts.examined}; "
        f"returned: {counts.returned}; graph VMs: {counts.returned}; "
        f"truncated: {str(data.truncated).lower()}.",
        "",
        "Storage/network edges denote observed backing or attachment, not traffic paths. "
        "Firewall rules, ALLOW/DROP decisions, traffic paths and host metrics were not examined. "
        "This is a measured inventory sketch, not a proposed design.",
        "",
    ]
    if data.complete and counts.enumerated == 0:
        lines.extend(["The selected folder has no VMs under this membership mode.", ""])
    if data.errors:
        lines.extend(["### Collection errors", ""])
        for error in sorted(data.errors, key=lambda item: (
                item.object_id or "", item.relationship, item.error)):
            lines.append(f"- {markdown_text(error.object_id or 'collection')}: "
                         f"{markdown_text(error.relationship)}: {markdown_text(error.error)}")
        lines.append("")
    lines.extend([
        "| VM ID | Exact VM name | Inventory path | Observed host | Datastore backing | vNIC network backing |",
        "|---|---|---|---|---|---|",
    ])
    for vm in sorted(data.vms, key=lambda item: (item.path, item.id)):
        networks = "; ".join(
            f"{nic.key}: {nic.network.name} ({nic.network.kind}, {nic.network.id})"
            if nic.network else f"{nic.key}: no resolved network (see backing/errors)"
            for nic in sorted(vm.nics, key=lambda item: item.key))
        cells = [vm.id, vm.name, vm.path,
                 f"{vm.host.name} ({vm.host.id})" if vm.host else "No observed host (see errors)",
                 "; ".join(f"{ds.name} ({ds.id})" for ds in sorted(
                     {ds.id: ds for ds in vm.datastores}.values(), key=lambda ds: ds.id)),
                 networks]
        lines.append("| " + " | ".join(markdown_text(cell) for cell in cells) + " |")
    if counts.returned != counts.enumerated and not data.truncated:
        lines.extend(["", "Diagram source withheld: some enumerated VM identities could not be read."])
    else:
        lines.extend(["", "```mermaid", mermaid(graph), "```"])
    if image_url:
        lines.extend(["", f"![Measured infrastructure sketch]({image_url})"])
    if render_error:
        lines.extend(["", f"**Diagram image unavailable:** {markdown_text(render_error)}"])
    return "\n".join(lines)
