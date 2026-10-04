"""Only API evidence, never model synthesis, reaches measured diagrams."""

import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import orchestrator as o
import topology_diagram as t


IMAGE = "0123456789abcdef.png"
PUBLIC = "https://assistant.example.ts.net/diagrams"


def identity(moid, name="same name", path="/DC/vm/folder"):
    return {"id": moid, "name": name, "path": path}


def topology():
    shared = identity("datastore-1", "shared datastore", "/DC/datastore/shared")
    return {
        "schema_version": "folder-topology-v1",
        "source": "vcenter",
        "collected_at": "2026-10-04T21:54:01+00:00",
        "folder": identity("group-1", "Tn3-Pod1"),
        "recursive": True, "complete": True, "errors": [], "truncated": False,
        "counts": {"enumerated": 2, "examined": 2, "returned": 2},
        "vms": [
            {**identity("vm-2"), "host": None, "datastores": [shared],
             "nics": []},
            {**identity("vm-1"), "host": identity("host-1"), "datastores": [shared, shared],
             "nics": [
                 {"key": 3, "label": "opaque adapter",
                  "network": {**identity("opaque-id", "actual opaque ID"), "kind": "opaque"},
                  "backing": {"opaqueNetworkId": "opaque-id"}},
                 {"key": 2, "label": "dv adapter",
                  "network": {**identity("dvportgroup-1"), "kind": "distributed"},
                  "backing": {"switchUuid": "uuid", "portgroupKey": "dvportgroup-1"}},
                 {"key": 1, "label": "standard adapter",
                  "network": {**identity("network-1"), "kind": "standard"},
                  "backing": {"network": "network-1"}},
             ]},
        ],
    }


def run(awaitable):
    return asyncio.run(awaitable)


def test_stable_identity_dedup_count_and_provenance():
    graph = t.build_graph(topology())
    assert len([node for node in graph.nodes if node.kind == "vm"]) == 2
    assert len([node for node in graph.nodes if node.kind == "datastore"]) == 1
    assert len([edge for edge in graph.edges if edge.relationship == "datastore backing"]) == 2
    assert len([edge for edge in graph.edges if edge.relationship == "runtime host"]) == 1
    assert len([node for node in graph.nodes if node.kind == "opaque"]) == 1
    assert len({node.id for node in graph.nodes}) == len(graph.nodes)
    for edge in graph.edges:
        assert edge.evidence.source == "vcenter"
        assert edge.evidence.endpoint == "/folders/topology"
        assert edge.evidence.object_id
        assert edge.evidence.target_id
        assert edge.evidence.relationship
    assert not any(edge.source == t.node_id("host", "host-1")
                   for edge in graph.edges)
    assert {edge.relationship for edge in graph.edges} == {
        "member", "runtime host", "datastore backing", "vNIC", "network backing",
    }
    assert all(node.kind == "vm" for edge in graph.edges
               if edge.relationship in ("runtime host", "datastore backing")
               for node in graph.nodes if node.id == edge.source)
    source = t.mermaid(graph)
    assert all(invented not in source for invented in (
        "tn3-fw01", "tn3-bb", "tn3-nagios", "tn3-pod1-vm",
        "SNMP", "TCP80", "TCP443", "ALLOW", "DROP", "Backup Appliance",
    ))


def test_ordering_and_edge_dedup_are_deterministic():
    data = topology()
    shuffled = copy.deepcopy(data)
    shuffled["vms"].reverse()
    shuffled["vms"][0]["nics"].reverse()
    shuffled["vms"][0]["datastores"].reverse()
    assert t.mermaid(t.build_graph(data)) == t.mermaid(t.build_graph(shuffled))
    assert t.build_graph(data).model_dump() == t.build_graph(shuffled).model_dump()
    for kind in ("folder", "vm", "nic", "host", "standard"):
        assert t.node_id(kind, "foo") != t.node_id(kind, "foo ")
        assert t.node_id(kind, "foo") != t.node_id(kind, "Foo")


def test_hostile_labels_cannot_add_syntax_or_markdown():
    data = topology()
    attack = 'a"] --> evil["x\n%%{init: {"securityLevel":"loose"}}%%\n<script> & | ``` 🐈'
    data["folder"]["name"] = attack
    data["vms"][0]["name"] = attack
    graph = t.build_graph(data)
    source = t.mermaid(graph)
    assert source.count("\n") == len(graph.nodes) + len(graph.edges)
    assert "evil[" not in source and "%%" not in source and "<script>" not in source
    assert "#34;" in source and "#10;" in source and "#128008;" in source
    report = t.report(graph, "/folders/topology", None, None)
    assert report.count("```") == 2
    assert "<script>" not in report


def test_empty_is_not_incomplete_or_failed():
    data = topology()
    data.update(vms=[], counts={"enumerated": 0, "examined": 0, "returned": 0})
    assert "has no VMs" in t.report(t.build_graph(data), "source", None, None)
    data.update(complete=False, errors=[{
        "object_id": "group-1", "relationship": "childEntity", "error": "NoPermission",
    }])
    report = t.report(t.build_graph(data), "source", None, None)
    assert "INCOMPLETE" in report and "has no VMs" not in report
    assert "NoPermission" in report


@pytest.mark.parametrize("mutation", [
    {"counts": {"enumerated": 9, "examined": 9, "returned": 2}},
    {"counts": {"enumerated": 2, "examined": 2, "returned": 9}},
    {"errors": [{"object_id": "vm-1", "relationship": "datastore", "error": "denied"}]},
    {"truncated": True},
])
def test_false_completeness_or_inconsistent_counts_are_rejected(mutation):
    data = {**topology(), **mutation}
    with pytest.raises(ValueError):
        t.build_graph(data)


def test_partial_retains_only_observed_nodes_and_explicit_counts():
    data = topology()
    data.update(complete=False, counts={"enumerated": 3, "examined": 3, "returned": 2},
                errors=[{"object_id": "vm-3", "relationship": "name", "error": "denied"}])
    graph = t.build_graph(data)
    assert len([node for node in graph.nodes if node.kind == "vm"]) == 2
    report = t.report(graph, "source", None, None)
    assert "enumerated: 3" in report and "graph VMs: 2" in report
    assert "INCOMPLETE" in report
    assert not any(node.object_id == "vm-3" for node in graph.nodes)
    assert "```mermaid" not in report


def test_identity_conflict_is_not_arbitrarily_resolved():
    data = topology()
    data["vms"][0]["datastores"][0] = {
        **data["vms"][0]["datastores"][0], "name": "changed during collection",
    }
    with pytest.raises(ValueError, match="Conflicting identities"):
        t.build_graph(data)


@pytest.mark.parametrize("selector", [{}, {"path": "relative"}, {"name": " "},
                                      {"name": "x", "path": "/DC/vm/x"}, {"vm_name": "fuzzy"}])
def test_structured_selector_refuses_invalid_values(selector):
    with pytest.raises(ValueError):
        t.FolderSelector.model_validate(selector)


def fake_client(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(o.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


@pytest.fixture
def renderer(monkeypatch):
    calls = []

    async def render(source):
        calls.append(source)
        return IMAGE

    monkeypatch.setattr(o, "DIAGRAM_IMAGE", "local/mermaid-cli:test")
    monkeypatch.setattr(o, "DIAGRAM_PUBLIC_URL_BASE", PUBLIC)
    monkeypatch.setattr(o, "_render_one", render)
    return calls


def test_local_tool_progress_png_metadata_and_exact_selector(monkeypatch, renderer):
    requests = []

    def handler(request):
        requests.append(request)
        data = topology()
        data["recursive"] = False
        return httpx.Response(200, json=data)

    fake_client(monkeypatch, handler)
    progress = []

    async def notify(stage, message, details):
        progress.append(stage)

    result = run(o.call_api("folder_topology_diagram",
                           {"selector": {"folder_id": "group-1"}, "recursive": False},
                           progress=notify))
    assert requests[0].url.path == "/folders/topology"
    assert dict(requests[0].url.params) == {"folder_id": "group-1", "recursive": "false"}
    assert result["image_url"] == f"{PUBLIC}/{IMAGE}"
    assert renderer == [result["mermaid"]]
    assert result["source"]["complete"] is True
    assert progress == ["folder_resolution", "topology_collection",
                        "diagram_generation", "diagram_rendering"]


@pytest.mark.parametrize("status,detail,expected", [
    (404, "Not Found", "Update mcp-vcenter on Windows first"),
    (409, {"error": "ambiguous", "candidates": [{"id": "group-2", "path": "/DC2/vm/x"}]}, "/DC2/vm/x"),
    (404, "No matching folder (complete scan)", "No matching folder"),
    (503, "vCenter inaccessible", "vCenter inaccessible"),
])
def test_api_failures_remain_explicit(monkeypatch, renderer, status, detail, expected):
    fake_client(monkeypatch, lambda request: httpx.Response(status, json={"detail": detail}))
    result = run(o.folder_topology_diagram({"name": "Tn3-Pod1"}))
    assert expected in result["report_markdown"]
    assert "error" in result and "graph" not in result and renderer == []
    assert result["image_url"] is None
    assert result["source"]["complete"] is False
    assert result["source"]["counts"] is None


@pytest.mark.parametrize("public", ["", "/diagrams", "http://example/diagrams",
                                   "https://example/diagrams?public=yes", "https://[malformed"])
def test_no_relative_or_unprotected_image_urls(monkeypatch, renderer, public):
    fake_client(monkeypatch, lambda request: httpx.Response(200, json=topology()))
    monkeypatch.setattr(o, "DIAGRAM_PUBLIC_URL_BASE", public)
    result = run(o.folder_topology_diagram({"name": "Tn3-Pod1"}))
    assert result["image_url"] is None and "error" in result
    assert "No relative image URL was emitted" in result["report_markdown"]
    assert renderer == []


def test_renderer_failure_is_not_success_shaped(monkeypatch, renderer):
    fake_client(monkeypatch, lambda request: httpx.Response(200, json=topology()))

    async def fail(source):
        return None

    monkeypatch.setattr(o, "_render_one", fail)
    result = run(o.folder_topology_diagram({"name": "Tn3-Pod1"}))
    assert result["image_url"] is None
    assert "renderer failed" in result["error"]
    assert "Diagram image unavailable" in result["report_markdown"]
    assert result["source"]["counts"]["returned"] == 2


def test_png_population_must_match_enumerated_vms_unless_explicitly_truncated(monkeypatch, renderer):
    data = topology()
    data.update(complete=False, counts={"enumerated": 3, "examined": 3, "returned": 2},
                errors=[{"object_id": "vm-3", "relationship": "identity", "error": "denied"}])
    fake_client(monkeypatch, lambda request: httpx.Response(200, json=data))
    result = run(o.folder_topology_diagram({"name": "Tn3-Pod1"}))
    assert "withheld" in result["error"]
    assert result["image_url"] is None and result["mermaid"] is None
    assert "```mermaid" not in result["report_markdown"] and renderer == []
    data["truncated"] = True
    result = run(o.folder_topology_diagram({"name": "Tn3-Pod1"}))
    assert result["image_url"] == f"{PUBLIC}/{IMAGE}"
    assert "truncated: true" in result["report_markdown"]


def test_model_cannot_rewrite_final_report_even_in_read_only_run(monkeypatch, renderer):
    model_calls = []

    def handler(request):
        if request.url.path == "/api/chat":
            model_calls.append(json.loads(request.content))
            assert len(model_calls) == 1, "Model was allowed to rewrite the report"
            return httpx.Response(200, json={
                "message": {"role": "assistant", "content":
                            "Fabricated nine VMs. MCP=VCF Cloud Services. "
                            "tn3-fw01=Firewall/NSX Edge; tn3-bb=Backup Appliance; "
                            "tn3-nagios=Monitoring; tn3-pod1-vm=Production App. "
                            "SNMP/API traffic through firewall TCP80/443 ALLOW/DROP. "
                            "Export using mermaid.live.",
                            "tool_calls": [{"function": {
                                "name": "folder_topology_diagram",
                                "arguments": {"selector": {"name": "Tn3-Pod1"}},
                            }}]},
                "prompt_eval_count": 12, "eval_count": 5,
            })
        return httpx.Response(200, json=topology())

    fake_client(monkeypatch, handler)
    result = run(o.chat_with_tools("Diagram folder Tn3-Pod1", scope="vcenter", read_only=True))
    assert result["authoritative_diagram"] is True
    assert "Fabricated" not in result["answer"]
    assert all(text not in result["answer"] for text in (
        "tn3-fw01", "tn3-bb", "tn3-nagios", "tn3-pod1-vm", "mermaid.live",
        "SNMP/API", "TCP80/443", "VCF Cloud Services",
    ))
    assert "enumerated: 2" in result["answer"]
    assert result["tools_called"] == ["folder_topology_diagram"]
    assert result["usage"]["prompt_tokens"] == 12
    assert all(not o.TOOL_SPECS[tool["function"]["name"]]["write"]
               for tool in model_calls[0]["tools"])


@pytest.mark.parametrize("content", [
    "Nine VMs on host, datastore and FW. Export on mermaid.live.\n"
    "```mermaid\ngraph TD\n a[tn3-fw01] --> b[tn3-bb]\n```",
    "Invented topology ![diagram](/diagrams/0123456789abcdef.png)",
])
def test_freeform_model_diagrams_are_withheld_without_tool_evidence(monkeypatch, content):
    fake_client(monkeypatch, lambda request: httpx.Response(200, json={
        "message": {"role": "assistant", "content": content},
    }))
    answer = run(o.chat_with_tools("Diagram a folder"))["answer"]
    assert "Unverified infrastructure diagram withheld" in answer
    assert "tn3-fw01" not in answer and "mermaid.live" not in answer
    assert "```" not in answer and "![" not in answer


@pytest.mark.parametrize("selector,recursive", [
    ({"folder_id": "wrong-id"}, True),
    ({"path": "/wrong/path"}, True),
    ({"name": "wrong-name"}, True),
    ({"folder_id": "group-1"}, False),
])
def test_selector_or_membership_response_mismatch_is_rejected(monkeypatch, renderer, selector, recursive):
    fake_client(monkeypatch, lambda request: httpx.Response(200, json=topology()))
    result = run(o.folder_topology_diagram(selector, recursive))
    assert "different" in result["error"] and renderer == []
    assert "graph" not in result


@pytest.mark.parametrize("timestamp", ["not a time", "2026-10-04T12:00:00",
                                      "2026-10-04T12:00:00+02:00"])
def test_collection_timestamp_must_be_utc(timestamp):
    with pytest.raises(ValueError):
        t.build_graph({**topology(), "collected_at": timestamp})


def test_passthrough_preserves_parallel_pending_approvals(monkeypatch, renderer):
    pending = {"confirmation_token": "token", "tool": "vcenter_vm_power",
               "arguments": {}, "action": "power off"}

    def handler(request):
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {
                "role": "assistant", "content": "",
                "tool_calls": [
                    {"function": {"name": "folder_topology_diagram",
                                  "arguments": {"selector": {"name": "Tn3-Pod1"}}}},
                    {"function": {"name": "vcenter_vm_power", "arguments": {}}},
                ],
            }})
        return httpx.Response(200, json=topology())

    fake_client(monkeypatch, handler)
    original = o.call_api

    async def call(name, arguments, **kwargs):
        if name == "vcenter_vm_power":
            return pending
        return await original(name, arguments, **kwargs)

    monkeypatch.setattr(o, "call_api", call)
    result = run(o.chat_with_tools("diagram and propose a power change"))
    assert result["pending_actions"] == [pending]
    assert result["authoritative_diagram"] is True


def test_tool_scopes_and_read_only_spec():
    spec = o.TOOL_SPECS["folder_topology_diagram"]
    assert spec["local"] == "folder_topology_diagram"
    assert spec["write"] is False and spec["system"] == "vcenter"
    for scope in ("all", "vcenter"):
        schema = next(tool["function"] for tool in o.TOOLS_BY_SCOPE[scope]
                      if tool["function"]["name"] == "folder_topology_diagram")
        assert schema["parameters"]["required"] == ["selector"]
        assert schema["parameters"]["properties"]["selector"]["additionalProperties"] is False


def test_both_client_formats_and_stream_preserve_authoritative_answer(monkeypatch, renderer, tmp_path):
    answer = "## Exact measured report\n\n```mermaid\nflowchart LR\n a[\"VM\"]\n```\n\n" \
             f"![Measured infrastructure sketch]({PUBLIC}/{IMAGE})"

    async def chat(*args, **kwargs):
        return {"answer": answer, "authoritative_diagram": True,
                "usage": {}, "tools_called": ["folder_topology_diagram"]}

    async def installed():
        return []

    async def telemetry():
        return {}

    monkeypatch.setattr(o, "chat_with_tools", chat)
    monkeypatch.setattr(o, "_installed_models", installed)
    monkeypatch.setattr(o, "fetch_telemetry", telemetry)
    monkeypatch.setattr(o.store, "DB_PATH", str(tmp_path / "state.db"))
    o.store.init_db()
    api = TestClient(o.app)
    custom = api.post("/chat", json={"message": "diagram", "scope": "vcenter"})
    assert custom.status_code == 200, custom.text
    assert custom.json()["answer"] == answer
    request = {"model": "assistant-vcenter", "messages": [{"role": "user", "content": "diagram"}]}
    openai = api.post("/v1/chat/completions", json=request)
    assert openai.status_code == 200
    assert openai.json()["choices"][0]["message"]["content"] == answer
    streamed = api.post("/v1/chat/completions", json={**request, "stream": True})
    events = [json.loads(line[6:]) for line in streamed.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    content = "".join(event["choices"][0]["delta"].get("content", "") for event in events)
    assert content == answer
    assert renderer == [], "Authoritative output was rewritten or rendered twice"
    conversation = o.store.history(custom.json()["conversation_id"])
    assert conversation[-1]["content"] == answer


def test_absolute_image_uses_existing_authenticated_diagrams_route(monkeypatch, tmp_path):
    monkeypatch.setattr(o, "DIAGRAM_AUTH", "tailscale")
    monkeypatch.setattr(o, "DIAGRAM_DIR", str(tmp_path))
    (tmp_path / IMAGE).write_bytes(b"PNG")
    api = TestClient(o.app)
    # Both clients receive this same absolute URL. Tailscale Serve already
    # routes /diagrams directly to :8090, regardless of the chat client's origin.
    url = f"{PUBLIC}/{IMAGE}"
    assert api.get(url).status_code == 403
    valid = api.get(url, headers={"Tailscale-User-Login": "user@example.com"})
    assert valid.status_code == 200 and valid.content == b"PNG"
    assert valid.headers["content-type"] == "image/png"
    assert api.get(f"{PUBLIC}/invalid.png",
                   headers={"Tailscale-User-Login": "user@example.com"}).status_code == 404
