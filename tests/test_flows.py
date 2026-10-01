"""Flow resolution against a fake Network Insight.

Field names mirror a real flow entity read off the live system: source_vm,
destination_vm, traffic_type as an enum like EAST_WEST_TRAFFIC, and a port
object rather than a scalar.
"""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vcfNetworks"))
os.environ.setdefault("NI_HOST", "fake")
os.environ.setdefault("NI_USERNAME", "u")
os.environ.setdefault("NI_PASSWORD", "p")

import vcf_networks_api as m  # noqa: E402


@pytest.fixture(autouse=True)
def clear_flow_cache(monkeypatch):
    m._reset_flow_inventory_cache()
    monkeypatch.setattr(m, "FLOW_INVENTORY_CACHE_TTL", 60.0)
    monkeypatch.setattr(m, "FLOW_INVENTORY_CACHE_MAX_ENTRIES", 32)


class FakeNI:
    """Caps flow pages at 10 whatever size is asked for, like the real server."""

    def __init__(self, flows):
        self.flows = flows
        self.detail_calls = 0

    def request(self, method, path, **kw):
        if path.startswith("/api/ni/entities/flows/"):
            self.detail_calls += 1
            flow_id = unquote(path.rsplit("/", 1)[-1])
            return self.flows[flow_id]

        params = kw.get("params", {})
        size = params.get("size", 10)
        start = int(params.get("cursor") or 0)
        ids = list(self.flows)
        page = max(0, min(size, 10, len(ids) - start))
        results = [{"entity_id": i} for i in ids[start:start + page]]
        nxt = start + page
        return {
            "results": results,
            "cursor": str(nxt) if nxt < len(ids) else None,
            "total_count": len(ids),
        }


def make_flow(idx, traffic_type, src_vm, dst_vm=None):
    flow = {
        "entity_id": f"10000:515:{idx}",
        "name": f"flow-{idx}",
        "source_ip": {"ip_address": f"192.0.2.{idx}"},
        "destination_ip": {"ip_address": "8.8.8.8" if dst_vm is None else "192.0.2.99"},
        "port": {"start": 443, "display": "443", "iana_port_display": "443 [https]"},
        "protocol": "TCP",
        "traffic_type": traffic_type,
        "firewall_action": "ALLOW",
        "within_host": False,
        "source_vm": {"entity_name": src_vm},
        "source_l2_network": {"entity_name": "vlan-1000"},
    }
    if dst_vm:
        flow["destination_vm"] = {"entity_name": dst_vm}
    return flow


def build(count_ns=6, count_ew=45):
    flows = {}
    for i in range(count_ns):
        f = make_flow(i, "NORTH_SOUTH_TRAFFIC", f"web{i}")
        flows[f["entity_id"]] = f
    for i in range(count_ns, count_ns + count_ew):
        f = make_flow(i, "EAST_WEST_TRAFFIC", f"app{i}", dst_vm=f"db{i}")
        flows[f["entity_id"]] = f
    return flows


def install(flows):
    m.client = FakeNI(flows)
    return m.client


def test_flow_record_uses_real_field_names():
    rec = m.flow_record(make_flow(1, "EAST_WEST_TRAFFIC", "rhel9-ipxe", dst_vm="adc01"))
    assert rec["source_vm"] == "rhel9-ipxe"
    assert rec["destination_vm"] == "adc01"
    assert rec["source_ip"] == "192.0.2.1"
    assert rec["protocol"] == "TCP"
    assert rec["traffic_type"] == "EAST_WEST_TRAFFIC"
    assert rec["port"] == "443 [https]"


def test_absent_destination_vm_is_omitted_not_nulled():
    """An external destination has no destination_vm, and that absence matters."""
    rec = m.flow_record(make_flow(2, "NORTH_SOUTH_TRAFFIC", "web0"))
    assert "destination_vm" not in rec
    assert rec["destination_ip"] == "8.8.8.8"


def test_resolves_every_flow_across_pages():
    """51 flows arrive 10 at a time; all of them must be resolved."""
    fake = install(build())
    out = m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)
    assert out["flows_examined"] == 51
    assert out["total_flows_known"] == 51
    assert out["truncated"] is False
    assert fake.detail_calls == 51


def test_breakdown_counts_whole_window_not_the_filtered_subset():
    install(build())
    out = m.flows_inventory(hours=1, limit=100, traffic_type="north_south", vm=None)
    assert out["traffic_type_breakdown"] == {
        "NORTH_SOUTH_TRAFFIC": 6,
        "EAST_WEST_TRAFFIC": 45,
    }
    assert out["flow_count"] == 6
    assert all(f["traffic_type"] == "NORTH_SOUTH_TRAFFIC" for f in out["flows"])


def test_traffic_type_filter_accepts_loose_spellings():
    for spelling in ("north_south", "NORTH-SOUTH", "north south", "NORTH_SOUTH_TRAFFIC"):
        install(build())
        out = m.flows_inventory(hours=1, limit=100, traffic_type=spelling, vm=None)
        assert out["flow_count"] == 6, spelling


def test_vm_filter_matches_source_or_destination():
    install(build())
    out = m.flows_inventory(hours=1, limit=100, traffic_type=None, vm="db10")
    assert out["flow_count"] == 1
    assert out["flows"][0]["destination_vm"] == "db10"


def test_truncation_is_measured_against_the_estate():
    """The bug that made 10 of 57 look complete, in flow form."""
    install(build())
    out = m.flows_inventory(hours=1, limit=20, traffic_type=None, vm=None)
    assert out["flows_examined"] == 20
    assert out["total_flows_known"] == 51
    assert out["truncated"] is True
    assert "20 of 51" in out["hint"]


def test_filtering_to_nothing_does_not_claim_truncation():
    install(build())
    out = m.flows_inventory(hours=1, limit=100, traffic_type=None, vm="nosuchvm")
    assert out["flow_count"] == 0
    assert out["truncated"] is False
    assert out["hint"] is None


def test_equivalent_inventory_request_hits_cache_and_returns_a_copy():
    fake = install(build(count_ns=1, count_ew=0))
    first = m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)
    first["flows"][0]["source_vm"] = "mutated"

    second = m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)

    assert fake.detail_calls == 1
    assert second["flows"][0]["source_vm"] == "web0"


def test_cache_expires(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(m, "FLOW_INVENTORY_CACHE_TTL", 5.0)
    fake = install(build(count_ns=1, count_ew=0))

    m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)
    clock[0] += 6
    m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)

    assert fake.detail_calls == 2


def test_zero_ttl_disables_cache(monkeypatch):
    monkeypatch.setattr(m, "FLOW_INVENTORY_CACHE_TTL", 0.0)
    fake = install(build(count_ns=1, count_ew=0))

    m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)
    m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)

    assert fake.detail_calls == 2


def test_cache_key_includes_every_filter():
    fake = install(build(count_ns=2, count_ew=1))

    m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)
    m.flows_inventory(hours=2, limit=100, traffic_type=None, vm=None)
    m.flows_inventory(hours=1, limit=2, traffic_type=None, vm=None)
    m.flows_inventory(hours=1, limit=100, traffic_type="north_south", vm=None)
    m.flows_inventory(hours=1, limit=100, traffic_type=None, vm="web0")

    assert fake.detail_calls == 3 + 3 + 2 + 3 + 3


def test_failed_flow_resolution_is_not_cached():
    class FlakyNI(FakeNI):
        def __init__(self, flows):
            super().__init__(flows)
            self.fail_once = True

        def request(self, method, path, **kw):
            if path.startswith("/api/ni/entities/flows/") and self.fail_once:
                self.fail_once = False
                self.detail_calls += 1
                raise m.HTTPException(status_code=502, detail="temporary failure")
            return super().request(method, path, **kw)

    fake = FlakyNI(build(count_ns=1, count_ew=0))
    m.client = fake

    first = m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)
    second = m.flows_inventory(hours=1, limit=100, traffic_type=None, vm=None)

    assert first["flows"][0]["error"] == "could not fetch flow detail"
    assert second["flows"][0]["source_vm"] == "web0"
    assert fake.detail_calls == 2


def test_concurrent_identical_requests_share_one_resolution():
    started = threading.Event()
    release = threading.Event()

    class BlockingNI(FakeNI):
        def request(self, method, path, **kw):
            if path.startswith("/api/ni/entities/flows/"):
                started.set()
                release.wait(timeout=2)
            return super().request(method, path, **kw)

    fake = BlockingNI(build(count_ns=1, count_ew=0))
    m.client = fake
    args = {"hours": 1, "limit": 100, "traffic_type": None, "vm": None}

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(m.flows_inventory, **args)
        assert started.wait(timeout=1)
        second = pool.submit(m.flows_inventory, **args)
        time.sleep(0.02)
        assert not second.done()
        release.set()
        assert first.result()["flow_count"] == 1
        assert second.result()["flow_count"] == 1

    assert fake.detail_calls == 1
