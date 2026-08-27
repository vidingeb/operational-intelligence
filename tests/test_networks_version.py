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


def _patch(monkeypatch, payload):
    monkeypatch.setattr(m.client, "request", lambda method, path, **kw: payload)


def test_api_version_is_reported_as_an_api_version(monkeypatch):
    _patch(monkeypatch, LIVE_VERSION)
    out = m.version()

    assert out["api_version"] == "9.0.2.0"
    assert out["fields_returned_by_server"] == ["api_version"]


def test_missing_product_version_is_stated_not_faked(monkeypatch):
    _patch(monkeypatch, LIVE_VERSION)
    out = m.version()

    assert out["product_version"] is None
    assert "do not present it as the product version" in out["product_version_note"]
    assert "version" not in out or out.get("version") is None


def test_a_real_product_version_would_be_used(monkeypatch):
    """If a future release starts returning one, report it and drop the note."""
    _patch(monkeypatch, {"api_version": "9.0.2.0", "version": "6.14.0"})
    out = m.version()

    assert out["version"] == "6.14.0"
    assert "product_version_note" not in out


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


def test_unexpected_shape_is_reported(monkeypatch):
    _patch(monkeypatch, ["not", "a", "dict"])

    assert m.version()["unexpected_shape"] is True
