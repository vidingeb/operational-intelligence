"""Veeam version reporting.

The payload here is verbatim from veeam01 (build 13.0.1.1071). It matters that
two of its most interesting values are falsy — patches is an empty list and
isRegistered is false — because the obvious way to drop empty fields also
drops those, turning "this backup server is unregistered and unpatched" into
silence.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "veeam"))
os.environ.setdefault("VEEAM_URL", "https://fake:9419")
os.environ.setdefault("VEEAM_USER", "u")
os.environ.setdefault("VEEAM_PASSWORD", "p")

import veeam_api as m  # noqa: E402


LIVE_SERVER_INFO = {
    "platform": "Windows",
    "vbrId": "482b8c1f-8a1e-4005-b12d-d58b0624fa9f",
    "name": "veeam01",
    "buildVersion": "13.0.1.1071",
    "patches": [],
    "databaseVendor": "PostgreSQL",
    "sqlServerEdition": "",
    "sqlServerVersion": "PostgreSQL 15.6, compiled by Visual C++ build 1937, 64-bit",
    "databaseSchemaVersion": "9814",
    "databaseContentVersion": "9814",
    "veeamRegistration": {"isRegistered": False},
}


def _live(monkeypatch, payload=None):
    monkeypatch.setattr(m, "request", lambda method, path, **kw: payload or LIVE_SERVER_INFO)
    monkeypatch.setitem(m._session, "api_version", "1.3-rev0")
    m._session.pop("version_unconfirmed", None)
    return m.version()


def test_parses_the_live_payload(monkeypatch):
    out = _live(monkeypatch)

    assert out["build_version"] == "13.0.1.1071"
    assert out["name"] == "veeam01"
    assert out["database_vendor"] == "PostgreSQL"
    assert out["platform"] == "Windows"
    assert out["rest_api_version"] == "1.3-rev0"


def test_unregistered_is_reported_not_dropped(monkeypatch):
    """False must survive the empty-value filter — it is the whole point."""
    out = _live(monkeypatch)

    assert "registered" in out
    assert out["registered"] is False


def test_zero_patches_is_reported_not_dropped(monkeypatch):
    out = _live(monkeypatch)

    assert out["patches_applied"] == 0


def test_empty_strings_are_omitted(monkeypatch):
    """sqlServerEdition is "" on this server; an empty value is not a fact."""
    out = _live(monkeypatch)

    assert all(v != "" for k, v in out.items() if isinstance(v, str))


def test_registration_absent_means_no_claim(monkeypatch):
    payload = dict(LIVE_SERVER_INFO)
    payload.pop("veeamRegistration")

    out = _live(monkeypatch, payload)

    assert "registered" not in out, "absence of the field must not imply unregistered"


def test_unconfirmed_api_version_is_admitted(monkeypatch):
    monkeypatch.setattr(m, "request", lambda method, path, **kw: LIVE_SERVER_INFO)
    monkeypatch.setitem(m._session, "api_version", "1.3-rev0")
    monkeypatch.setitem(m._session, "version_unconfirmed", ["1.2-rev0 -> 400"])

    assert m.version()["api_version_confirmed"] is False


def test_server_field_names_are_returned(monkeypatch):
    out = _live(monkeypatch)

    assert "buildVersion" in out["fields_returned_by_server"]
    assert out["raw"] == LIVE_SERVER_INFO


def test_unexpected_shape_is_reported(monkeypatch):
    out = _live(monkeypatch, ["not", "a", "dict"])

    assert out["unexpected_shape"] is True
