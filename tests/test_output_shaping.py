"""Output shaping and triage condensing.

Both exist because of things seen in the running system rather than
anticipated: a Markdown table rendered as literal pipes in a plain-text
chat pane, and a single estate question costing 49,518 input tokens.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))
os.environ.setdefault("MCP_SERVER", "http://fake")
os.environ.setdefault("OLLAMA_URL", "http://fake")

import orchestrator as o  # noqa: E402


# --- plain_text --------------------------------------------------------------

def test_html_break_is_removed():
    assert "<br>" not in o.plain_text("one<br>two")
    assert "<br/>" not in o.plain_text("one<br/>two")


def test_table_separator_row_is_dropped():
    table = "| Source | Findings |\n|--------|----------|\n| vCenter | 3 alarms |"
    out = o.plain_text(table)
    assert "|--------|" not in out
    assert "---" not in out


def test_table_rows_become_readable_lines():
    table = "| Source | Findings |\n|--------|----------|\n| vCenter | 3 alarms |"
    out = o.plain_text(table)
    assert "Source  -  Findings" in out
    assert "vCenter  -  3 alarms" in out
    assert "|" not in out


def test_ordinary_prose_is_left_alone():
    text = "esx03: 94% memory\n\n- three VMs ballooning\n- no snapshots"
    assert o.plain_text(text) == text


def test_empty_answer_survives():
    assert o.plain_text("") == ""
    assert o.plain_text(None) is None


# --- _condense ---------------------------------------------------------------

def test_long_list_is_sampled_with_the_true_count():
    section = {"alarms": [{"n": i} for i in range(40)], "status": "ok"}
    out = o._condense(section, keep=5)
    assert out["count"] == 40
    assert len(out["alarms"]) == 5
    assert out["showing"] == "5 of 40"


def test_short_list_is_left_whole_and_unflagged():
    section = {"alarms": [{"n": 1}, {"n": 2}]}
    out = o._condense(section, keep=5)
    assert len(out["alarms"]) == 2
    assert "showing" not in out
    assert "more_available" not in out


def test_sampling_says_it_is_a_sample():
    """The model must not describe a sample as the complete set."""
    out = o._condense({"events": list(range(100))}, keep=3)
    assert "more_available" in out
    assert "complete" in out["more_available"].lower()


def test_failed_section_is_never_condensed_away():
    """A failure must stay visible or the estate looks healthier than it is."""
    section = {"error": "connection refused", "alarms": []}
    assert o._condense(section) == section


def test_bare_list_is_condensed_too():
    out = o._condense(list(range(30)), keep=4)
    assert out["count"] == 30
    assert len(out["sample"]) == 4


def test_scalar_sections_pass_through():
    assert o._condense("ok") == "ok"
    assert o._condense(7) == 7


def test_section_without_a_known_result_key_is_untouched():
    section = {"cpu_percent": 91, "memory_percent": 78}
    assert o._condense(section) == section


# --- build_welcome -----------------------------------------------------------
#
# The greeting was hardcoded and named three systems and an 8B/70B model
# choice long after both had changed. It is the first thing anyone reads.

import web_ui  # noqa: E402


def _cfg(**kw):
    base = {
        "systems": [{"key": "vcenter", "label": "vCenter", "summary": ""},
                    {"key": "logs", "label": "Logs", "summary": ""},
                    {"key": "backup", "label": "Veeam", "summary": ""}],
        "tool_count": 64,
        "write_tools_enabled": False,
    }
    base.update(kw)
    return base


def test_welcome_names_every_configured_system():
    text = web_ui.build_welcome(_cfg())
    for label in ("vCenter", "Logs", "Veeam"):
        assert label in text


def test_welcome_does_not_invent_systems_when_config_is_missing():
    """An unreachable orchestrator must not produce a confident wrong list."""
    text = web_ui.build_welcome({})
    assert "vCenter" not in text
    assert "could not reach" in text.lower()


def test_welcome_states_read_only_when_writes_are_disabled():
    assert "read-only" in web_ui.build_welcome(_cfg()).lower()


def test_welcome_promises_confirmation_when_writes_are_enabled():
    text = web_ui.build_welcome(_cfg(write_tools_enabled=True))
    assert "confirmation" in text.lower()
    assert "read-only" not in text.lower()


def test_welcome_reports_the_real_tool_count():
    assert "64 tools" in web_ui.build_welcome(_cfg())


def test_single_system_reads_correctly():
    text = web_ui.build_welcome(_cfg(systems=[{"label": "vCenter"}]))
    assert "query vCenter" in text
    assert " and " not in text.split("Try asking")[0]
