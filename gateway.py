"""
Unified API Gateway — single entry point for all VMware operational APIs.

Mounts the three existing FastAPI services under prefixed paths and exposes
a combined dashboard at the root so you have one place to check everything.

Start with:
    uvicorn gateway:app --host 0.0.0.0 --port 8000
"""

import os
from typing import Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from vcenter.vcenter_api import app as vcenter_app
from vcfops.vcf_ops_api import app as vcfops_app
from vcfNetworks.vcf_networks_api import app as vcfnetworks_app

app = FastAPI(
    title="Operational Intelligence — Unified Gateway",
    version="1.0.0",
    description=(
        "Single entry point combining vCenter, VCF Operations, and "
        "VCF Networks APIs. All existing endpoints are available under "
        "/vcenter, /ops, and /ni prefixes."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Mount the three sub-applications
# ---------------------------------------------------------------------------
app.mount("/vcenter", vcenter_app)
app.mount("/ops", vcfops_app)
app.mount("/ni", vcfnetworks_app)


# ---------------------------------------------------------------------------
# Unified endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "service": "Operational Intelligence — Unified Gateway",
        "version": "1.0.0",
        "backends": {
            "vcenter": {
                "prefix": "/vcenter",
                "description": "vCenter API (VMs, hosts, clusters, snapshots, datastores, actions)",
            },
            "vcf_operations": {
                "prefix": "/ops",
                "description": "VCF Operations / Aria Operations (alerts, resources, cost, reports)",
            },
            "vcf_networks": {
                "prefix": "/ni",
                "description": "VCF Operations for Networks (search, NSX segments, paths, alerts)",
            },
        },
        "unified_endpoints": [
            "GET  /health       — combined health check for all backends",
            "GET  /dashboard    — aggregated operational summary",
            "POST /chat         — ask questions in natural language (Gemma 4 26B via Ollama)",
            "GET  /chat/models  — list available Ollama models",
        ],
    }


@app.get("/health")
def unified_health():
    """Quick reachability check for all three backends."""
    statuses = {}

    # vCenter
    try:
        from vcenter.vcenter_api import health as vc_health
        statuses["vcenter"] = vc_health()
    except Exception as exc:
        statuses["vcenter"] = {"status": "error", "detail": str(exc)}

    # VCF Operations
    try:
        from vcfops.vcf_ops_api import health as ops_health
        statuses["vcf_operations"] = ops_health()
    except Exception as exc:
        statuses["vcf_operations"] = {"status": "error", "detail": str(exc)}

    # VCF Networks
    try:
        from vcfNetworks.vcf_networks_api import health as ni_health
        statuses["vcf_networks"] = ni_health()
    except Exception as exc:
        statuses["vcf_networks"] = {"status": "error", "detail": str(exc)}

    all_ok = all(
        s.get("status") == "ok" for s in statuses.values()
    )

    return {"status": "ok" if all_ok else "degraded", "backends": statuses}


@app.get("/dashboard")
def dashboard():
    """
    Aggregated operational summary across all three backends.

    Pulls key metrics from each service and returns a single snapshot
    so you can see the state of your entire VMware estate at a glance.
    """
    result = {
        "vcenter": {},
        "vcf_operations": {},
        "vcf_networks": {},
    }

    # --- vCenter ----------------------------------------------------------
    try:
        from vcenter.vcenter_api import (
            list_hosts,
            list_vms,
            list_clusters,
            list_datastores,
            active_alarms,
            old_snapshots,
        )

        hosts = list_hosts()
        vms = list_vms()
        clusters = list_clusters()
        datastores = list_datastores()
        alarms = active_alarms()
        snapshots = old_snapshots(days=14)

        powered_on = sum(1 for v in vms if v.get("power_state") == "poweredOn")
        powered_off = sum(1 for v in vms if v.get("power_state") == "poweredOff")

        low_free = [
            d for d in datastores if d.get("used_percent", 0) > 80
        ]

        result["vcenter"] = {
            "host_count": len(hosts),
            "vm_count": len(vms),
            "vms_powered_on": powered_on,
            "vms_powered_off": powered_off,
            "cluster_count": len(clusters),
            "datastore_count": len(datastores),
            "datastores_above_80_pct": len(low_free),
            "active_alarms": len(alarms),
            "old_snapshots_14d": len(snapshots),
        }
    except Exception as exc:
        result["vcenter"] = {"error": str(exc)}

    # --- VCF Operations ---------------------------------------------------
    try:
        from vcfops.vcf_ops_api import summary as ops_summary
        result["vcf_operations"] = ops_summary()
    except Exception as exc:
        result["vcf_operations"] = {"error": str(exc)}

    # --- VCF Networks -----------------------------------------------------
    try:
        from vcfNetworks.vcf_networks_api import alerts as ni_alerts
        alerts_data = ni_alerts()
        alert_count = (
            len(alerts_data) if isinstance(alerts_data, list) else
            alerts_data.get("total_count", alerts_data.get("results", None))
        )
        result["vcf_networks"] = {
            "alerts": alert_count,
            "raw": alerts_data if alert_count is None else None,
        }
        # Drop the raw key if we got a proper count
        if alert_count is not None:
            result["vcf_networks"].pop("raw", None)
    except Exception as exc:
        result["vcf_networks"] = {"error": str(exc)}

    return result


# ---------------------------------------------------------------------------
# On-prem AI assistant — Gemma 4 26B via Ollama
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:27b")

SYSTEM_PROMPT = """\
You are an on-prem VMware operations assistant. You answer questions about \
the infrastructure using the live data provided in the context below. \
Be concise, specific, and reference actual names/numbers from the data. \
If the data does not contain enough information to answer, say so.\
"""


class ChatRequest(BaseModel):
    message: str
    include_context: bool = True
    model: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    model: str
    context_included: bool


def _gather_context() -> str:
    """Collect a lightweight snapshot from the dashboard for the LLM."""
    try:
        data = dashboard()
    except Exception:
        return "(Could not retrieve live infrastructure context.)"

    import json
    return json.dumps(data, indent=2, default=str)


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest):
    """
    Ask a natural-language question about your VMware infrastructure.

    The gateway gathers a live dashboard snapshot and sends it alongside
    your question to a local Gemma 4 26B model running on Ollama.
    """
    model = body.model or OLLAMA_MODEL

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if body.include_context:
        context = _gather_context()
        messages.append({
            "role": "system",
            "content": f"Live infrastructure snapshot:\n{context}",
        })

    messages.append({"role": "user", "content": body.message})

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={"model": model, "messages": messages, "stream": False},
        )
        resp.raise_for_status()

    reply = resp.json().get("message", {}).get("content", "")

    return ChatResponse(
        reply=reply,
        model=model,
        context_included=body.include_context,
    )


@app.get("/chat/models")
async def chat_models():
    """List models available on the local Ollama instance."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        resp.raise_for_status()

    models = [m["name"] for m in resp.json().get("models", [])]
    return {"ollama_url": OLLAMA_BASE_URL, "default_model": OLLAMA_MODEL, "available": models}
