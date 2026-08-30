"""Network Insight version reporting.

The live payload is exactly {"api_version": "9.0.2.0"} — no product version,
no build. Two things follow, and both were wrong in the shipped code:

  /ni/health read upstream.get("version") from this payload, so it reported
  upstream_version: null on every call since it was written.

  An API version is not a product version. Labelling 9.0.2.0 as the Network
  Insight version produces an answer that is wrong even though the number is
  real — and next to Logs 9.0.2.0.25137850 it looks plausible enough to pass.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vcfNetworks"))
os.environ.setdefault("NI_BASE_URL", "https://fake")
os.environ.setdefault("NI_USERNAME", "u")
os.environ.setdefault("NI_PASSWORD", "p")

import vcf_networks_api as m  # noqa: E402


LIVE_VERSION = {"api_version": "9.0.2.0"}

# Shape and values verbatim from the live collector node, except the address,
# which is a documentation address (RFC 5737). The build, node_type and health
# are what the estate actually returned — those are what the tests assert on.
LIVE_NODE = {
    "id": "10000:901:4351403602829049222",
    "entity_type": "Node",
    "node_type": "PROXY_VM",
    "node_id": "I2BK8VIZSDKDM4JET8JL47AFW0",
    "ip_address": "192.0.2.169",
    "name": "Collector_192.0.2.169",
    "is_physical_flow_collector": False,
    "version": "9.0.2.0.25119537",
    "health": {"health_status": "HEALTHY",
               "health_details": [{"message": "SUCCEEDED", "code": "0"}]},
}
LIVE_NODE_LIST = {"results": [{"id": LIVE_NODE["id"], "entity_type": "Node"}],
                  "total_count": 1}


def _routes(monkeypatch, info=None, listing=None, detail=None):
    """Route the two-step node lookup: refs first, then each entity by id."""
    def fake(method, path, **kw):
        if path == "/api/ni/info/version":
            return info if info is not None else LIVE_VERSION
        if path == "/api/ni/infra/nodes":
            return listing if listing is not None else LIVE_NODE_LIST
        if path.startswith("/api/ni/infra/nodes/"):
            if isinstance(detail, Exception):
                raise detail
            return detail if detail is not None else LIVE_NODE
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(m.client, "request", fake)


def _patch(monkeypatch, payload):
    monkeypatch.setattr(m.client, "request", lambda method, path, **kw: payload)


def test_api_version_is_reported_as_an_api_version(monkeypatch):
    _routes(monkeypatch)
    out = m.version()

    assert out["api_version"] == "9.0.2.0"


def test_node_build_comes_from_the_node_record(monkeypatch):
    """The build is only reachable by fetching the entity by id."""
    _routes(monkeypatch)
    out = m.version()

    assert out["node_build"] == "9.0.2.0.25119537"
    assert out["nodes"][0]["name"] == "Collector_192.0.2.169"
    assert out["nodes"][0]["health_status"] == "HEALTHY"


def test_collector_build_is_not_called_the_platform_version(monkeypatch):
    """Live estate returns one PROXY_VM collector and no PLATFORM node.

    Reporting its build as "the Network Insight version" repeats the error
    this endpoint was just corrected for.
    """
    _routes(monkeypatch)
    out = m.version()

    assert out["node_types_reported"] == ["PROXY_VM"]
    assert "not necessarily the Network Insight platform" in out["platform_version_note"]


def test_platform_node_present_means_no_caveat(monkeypatch):
    platform = dict(LIVE_NODE, node_type="PLATFORM", name="Platform_192.0.2.168")
    _routes(monkeypatch, detail=platform)

    assert "platform_version_note" not in m.version()


def test_differing_node_builds_make_no_single_claim(monkeypatch):
    listing = {"results": [{"id": "a"}, {"id": "b"}], "total_count": 2}
    seen = []

    def fake(method, path, **kw):
        if path == "/api/ni/info/version":
            return LIVE_VERSION
        if path == "/api/ni/infra/nodes":
            return listing
        seen.append(path)
        return dict(LIVE_NODE, version=f"9.0.2.0.2511953{len(seen)}")

    monkeypatch.setattr(m.client, "request", fake)
    out = m.version()

    assert out["node_build"] is None
    assert "differing builds" in out["node_build_note"]


def test_one_node_failing_does_not_lose_the_rest(monkeypatch):
    _routes(monkeypatch, detail=RuntimeError("node gone"))
    out = m.version()

    assert out["nodes"][0]["error"].endswith("node gone")
    assert out["api_version"] == "9.0.2.0", "api_version must survive a node failure"


def test_node_listing_failure_is_reported(monkeypatch):
    def fake(method, path, **kw):
        if path == "/api/ni/info/version":
            return LIVE_VERSION
        raise RuntimeError("nodes unreachable")

    monkeypatch.setattr(m.client, "request", fake)
    out = m.version()

    assert "nodes unreachable" in out["node_lookup_error"]
    assert out["api_version"] == "9.0.2.0"


def test_health_reads_the_field_that_exists(monkeypatch):
    """upstream.get("version") returned null on every call for this payload."""
    _patch(monkeypatch, LIVE_VERSION)
    info = m.health()

    assert info["status"] == "ok"
    assert info["upstream_api_version"] == "9.0.2.0"
    assert "upstream_version" not in info


def test_health_reports_unreachable(monkeypatch):
    def boom(method, path, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(m.client, "request", boom)
    info = m.health()

    assert info["status"] == "unavailable"
    assert "connection refused" in info["error"]


def test_service_version_is_consistent():
    """Root and health reported 1.5.0 and 1.6.0 from the same process."""
    assert m.app.version == m.SERVICE_VERSION
    assert m.root()["version"] == m.SERVICE_VERSION


def test_raw_passthrough_reports_shape(monkeypatch):
    """Networks was the only wrapper without one, so every probe against NI
    needed a code change and a restart first."""
    _patch(monkeypatch, LIVE_NODE_LIST)

    out = m.raw(path="/api/ni/infra/nodes")

    assert out["path"] == "/api/ni/infra/nodes"
    assert out["top_level_keys"] == ["results", "total_count"]
    assert out["response"] == LIVE_NODE_LIST
