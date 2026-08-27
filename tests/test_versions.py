"""Version reporting across the estate.

The behaviour under test is as much about what the answer admits it does not
know as about the versions themselves: asked "what are we running", a partial
inventory presented as complete is worse than no answer.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))
import orchestrator as o  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_apis(monkeypatch):
    """Stand in for the five wrappers, recording which tools were called."""
    calls = []
    responses = {
        "vcenter_about": {"product": "VMware vCenter Server", "version": "9.0.1",
                          "build": "24755229"},
        "vcenter_list_hosts": {"hosts": [
            {"name": "esx01.vcf.local", "version": "9.0.1", "build": "24957456"},
            {"name": "esx02.vcf.local", "version": "9.0.1", "build": "24957456"},
        ]},
        "logs_version": {"product": "Logs", "version": "8.18.0"},
        "veeam_version": {"product": "Veeam Backup & Replication",
                          "build_version": "13.0.1.1071"},
        "networks_version": {"product": "Network Insight", "version": "6.14.0"},
    }

    async def fake_call(tool, args):
        calls.append(tool)
        if tool in responses:
            return responses[tool]
        raise RuntimeError(f"unexpected tool {tool}")

    monkeypatch.setattr(o, "call_api", fake_call)
    return calls, responses


def test_gathers_every_system_in_one_call(fake_apis):
    calls, _ = fake_apis
    result = run(o.estate_versions())

    assert set(calls) == {"vcenter_about", "vcenter_list_hosts", "logs_version",
                          "veeam_version", "networks_version"}
    assert result["vcenter"]["version"] == "9.0.1"
    assert result["veeam"]["build_version"] == "13.0.1.1071"


def test_hosts_are_grouped_by_build_not_listed_individually(fake_apis):
    result = run(o.estate_versions())
    hosts = result["esxi_hosts"]

    assert hosts["host_count"] == 2
    assert hosts["distinct_builds"] == 1
    assert hosts["builds"][0]["hosts"] == ["esx01.vcf.local", "esx02.vcf.local"]


def test_mixed_builds_are_visible(fake_apis):
    _, responses = fake_apis
    responses["vcenter_list_hosts"]["hosts"][1]["build"] = "24000000"

    hosts = run(o.estate_versions())["esxi_hosts"]

    assert hosts["distinct_builds"] == 2, "a host on a different build must not be hidden"


def test_uncovered_systems_are_named(fake_apis):
    result = run(o.estate_versions())

    assert "NSX" in result["not_covered"]
    assert "nsx" in result["guidance"].lower() or "not_covered" in result["guidance"]


def test_one_system_failing_does_not_lose_the_others(fake_apis, monkeypatch):
    _, responses = fake_apis

    async def fake_call(tool, args):
        if tool == "logs_version":
            raise RuntimeError("connection refused")
        return responses[tool]

    monkeypatch.setattr(o, "call_api", fake_call)
    result = run(o.estate_versions())

    assert result["sections_failed"] == ["logs"]
    assert result["veeam"]["build_version"] == "13.0.1.1071"


def test_host_lookup_failure_is_reported_not_swallowed(fake_apis, monkeypatch):
    _, responses = fake_apis

    async def fake_call(tool, args):
        if tool == "vcenter_list_hosts":
            raise RuntimeError("vcenter down")
        return responses[tool]

    monkeypatch.setattr(o, "call_api", fake_call)
    result = run(o.estate_versions())

    assert result["esxi_hosts"]["error"]
    assert "esxi_hosts" in result["sections_failed"]


def test_version_tools_are_registered_and_reachable():
    names = [t["name"] for t in o.REGISTRY]
    for tool in ("logs_version", "veeam_version", "networks_version",
                 "estate_versions", "vcenter_about"):
        assert tool in names, f"{tool} missing from REGISTRY"

    assert "estate_versions" in o.LOCAL_HANDLERS


def test_every_registry_entry_has_a_description():
    """Guards against an edit joining a URL and its description into one line.

    That mistake produces a syntactically valid entry whose url swallows the
    description, so the model is left choosing tools blind.
    """
    for tool in o.REGISTRY:
        assert tool["description"], f"{tool['name']} has no description"
        assert " " not in tool["url"], f"{tool['name']} url contains a space: {tool['url']}"


def test_prompt_forbids_offering_nonexistent_queries():
    assert "no tool for" in o.ENGINEER_RULES
