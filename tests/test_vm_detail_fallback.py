"""VCF Networks may report a VM FQDN while vCenter stores its short name."""
import asyncio

import httpx

import orchestrator as o


class FakeClient:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        status = self.statuses.pop(0)
        request = httpx.Request("GET", url, params=params)
        payload = {"name": params["name"]} if status == 200 else {"detail": "not found"}
        return httpx.Response(status, request=request, json=payload)


def test_fqdn_vm_detail_retries_short_name_after_not_found(monkeypatch):
    fake = FakeClient([404, 200])
    monkeypatch.setattr(o.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(o.call_api(
        "vcenter_vm_details",
        {"name": "vcfnetworks-collector.vcf.local"},
    ))

    assert result["name"] == "vcfnetworks-collector"
    assert [call[1]["name"] for call in fake.calls] == [
        "vcfnetworks-collector.vcf.local",
        "vcfnetworks-collector",
    ]


def test_short_vm_name_does_not_retry(monkeypatch):
    fake = FakeClient([404])
    monkeypatch.setattr(o.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(o.call_api(
        "vcenter_vm_details",
        {"name": "vcfnetworks-collector"},
    ))

    assert "error" in result
    assert len(fake.calls) == 1


def test_failed_short_name_retry_remains_a_failure(monkeypatch):
    fake = FakeClient([404, 404])
    monkeypatch.setattr(o.httpx, "AsyncClient", lambda **kwargs: fake)

    result = asyncio.run(o.call_api(
        "vcenter_vm_details",
        {"name": "missing.vcf.local"},
    ))

    assert "error" in result
    assert "404" in result["error"]
    assert len(fake.calls) == 2


def test_prompt_names_host_usage_tool_and_continues_read_only_work():
    assert "registered tool is vcenter_host_usage" in o.ENGINEER_RULES
    assert "Continue automatically with available read-only tools" in o.ENGINEER_RULES
