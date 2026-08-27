"""Log query construction and event flattening.

Every expectation here was measured against log01 rather than taken from
documentation. Four plausible constraint forms were rejected before the
working one was found, and "=" turned out to be unsupported on string
fields, so these tests exist to stop the code drifting back to the forms
that look right and fail.

The sample event is a verbatim record from the live server.
"""
import os
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vcfLogs"))
os.environ.setdefault("LOGS_URL", "https://fake:9543")
os.environ.setdefault("LOGS_PASSWORD", "p")

import vcf_logs_api as m  # noqa: E402


SAMPLE_EVENT = {
    "text": "2026-08-27T20:15:42Z esx01.vcf.local clusterAgent[2101833]: INFO\tClosing etcd client",
    "timestamp": 1787861742791,
    "timestampString": "2026-08-27 20:15:42.791 GMT+00:00",
    "fields": [
        {"name": "hostname", "startPosition": 21, "length": 15},
        {"name": "event_type", "content": "v4_d2e0f7b8"},
        {"name": "appname", "startPosition": 37, "length": 12},
        {"name": "procid", "startPosition": 50, "length": 7},
        {"name": "source", "content": "esx01.vcf.local"},
        {"name": "priority", "content": "notice"},
        {"name": "facility", "content": "kern"},
    ],
}


def test_field_is_its_own_path_segment():
    """timestamp>VALUE is rejected; timestamp/>VALUE works."""
    segment = m._segment("timestamp", ">", 123)
    assert segment.startswith("timestamp/")
    assert unquote(segment) == "timestamp/>123"


def test_operator_is_encoded_but_separator_is_not():
    segment = m._segment("timestamp", ">", 123)
    assert "%3E" in segment
    assert segment.count("/") == 1


def test_contains_keeps_the_space_before_the_value():
    """CONTAINS takes "CONTAINS value" — the space is part of the operator."""
    segment = m._segment("text", "CONTAINS ", "etcd")
    assert unquote(segment) == "text/CONTAINS etcd"
    assert "%20" in segment


def test_constraints_always_bound_the_time_window():
    query = m._constraints(hours=1)
    assert query.startswith("timestamp/")


def test_constraints_chain_with_slashes():
    query = unquote(m._constraints(hours=1, contains="error"))
    assert "/text/CONTAINS error" in query


def test_priority_uses_contains_not_equals():
    """log01 returns invalid_constraints for "=" on any string field."""
    query = unquote(m._constraints(hours=1, priority="err"))
    assert "priority/CONTAINS err" in query
    assert "priority/=" not in query


def test_positional_fields_are_sliced_out_of_the_text():
    """hostname has no "content" — it is offsets into the message."""
    resolved = m._resolve_fields(SAMPLE_EVENT)
    assert resolved["hostname"] == "esx01.vcf.local"
    assert resolved["appname"] == "clusterAgent"


def test_content_fields_are_read_directly():
    resolved = m._resolve_fields(SAMPLE_EVENT)
    assert resolved["priority"] == "notice"
    assert resolved["facility"] == "kern"


def test_shaped_event_carries_the_host():
    """Losing the hostname is the specific regression this guards."""
    shaped = m._shape(SAMPLE_EVENT)
    assert shaped["host"] == "esx01.vcf.local"
    assert shaped["priority"] == "notice"
    assert shaped["time"] == "2026-08-27 20:15:42.791 GMT+00:00"


def test_shaping_survives_an_event_with_no_fields():
    shaped = m._shape({"text": "bare message", "timestamp": 1})
    assert shaped["host"] is None
    assert shaped["text"] == "bare message"


def test_unexpected_shape_is_reported_not_swallowed(monkeypatch):
    """An empty result must never be mistaken for "no logs found"."""
    monkeypatch.setattr(m, "request", lambda *a, **k: {"errorMessage": "nope"})
    result = m._events(hours=1, limit=5)
    assert result["unexpected_shape"] is True
    assert result["raw"] == {"errorMessage": "nope"}


def test_truncation_is_flagged_when_the_limit_is_hit(monkeypatch):
    monkeypatch.setattr(m, "request",
                        lambda *a, **k: {"events": [SAMPLE_EVENT] * 5, "complete": True})
    result = m._events(hours=1, limit=5)
    assert result["truncated"] is True
    assert result["hint"]
