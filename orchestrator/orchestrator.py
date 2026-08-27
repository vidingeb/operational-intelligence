"""
On-prem AI Orchestrator — routes natural-language questions to vCenter,
VCF Operations, and VCF Networks APIs via an Ollama LLM.

Inference and data can live in different places. By default everything is
local, matching the original single-site deployment: the orchestrator runs on
the LLM VM (10.0.0.141) and calls APIs on the MCP server (10.0.0.140).

Set OLLAMA_URL to point inference somewhere else — for example a DGX Spark
GB10 reachable over a tailnet — and only prompts and tool results leave the
site. vCenter credentials and the API surface stay put.

Environment:
    OLLAMA_URL      Ollama endpoint       (default http://localhost:11434)
    MCP_SERVER      API host base URL     (default http://10.0.0.140)
    DEFAULT_MODEL   Model to use          (default llama3.1:8b)
    OLLAMA_TIMEOUT  Seconds, overrides the per-model default
    MAX_TOOL_ROUNDS Agentic tool-calling rounds  (default 5)
"""

import os
import re
import json
import urllib.parse
import time
import httpx
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="On-Prem AI Orchestrator", version="1.1")

# Configuration — env-overridable so the same code runs single- or split-site
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MCP_SERVER = os.getenv("MCP_SERVER", "http://10.0.0.140").rstrip("/")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "llama3.1:8b")

AVAILABLE_MODELS = {
    "llama3.1:8b": {"name": "Llama 3.1 8B", "description": "Fast (~30-60s) — good for daily use"},
    "hermes3": {"name": "Hermes 3", "description": "Fast (~30-60s) — optimized for tool calling"},
    "nemotron-3-nano:4b": {"name": "Nemotron 3 Nano 4B", "description": "Fast (~20-40s) — NVIDIA agent-optimized"},
    "qwen2.5:7b": {"name": "Qwen 2.5 7B", "description": "Fast (~20-40s) — excellent tool calling for its size"},
    "llama3.1:70b": {"name": "Llama 3.1 70B", "description": "Slow (~3-5min) — best accuracy"},
    "llama3.2": {"name": "Llama 3.2 3B", "description": "Fastest (~15-30s) — basic queries"},
    "gpt-oss:120b": {"name": "GPT-OSS 120B", "description": "Strongest multi-step tool calling — needs a GB10-class host"},
    "gpt-oss:20b": {"name": "GPT-OSS 20B", "description": "Same family at ~13 GB — fits a laptop, for portable/offline use"},
}

# Models big enough to need a long ceiling rather than the default
LARGE_MODEL_HINTS = ("70b", "120b")

# How many times the model may look at tool results and decide to call more.
# 1 would reduce this to the old single-shot behaviour, which breaks any
# question whose second lookup depends on the first one's answer.
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "5"))

# Per-tool-result budget fed back to the model, in characters.
TOOL_RESULT_LIMIT = int(os.getenv("TOOL_RESULT_LIMIT", "12000"))


def timeout_for(model: str) -> float:
    """Request timeout in seconds, overridable via OLLAMA_TIMEOUT."""
    override = os.getenv("OLLAMA_TIMEOUT")
    if override:
        return float(override)
    return 600.0 if any(h in model.lower() for h in LARGE_MODEL_HINTS) else 120.0

VCENTER_BASE = f"{MCP_SERVER}:8080"
OPS_BASE = f"{MCP_SERVER}:8081"
NETWORKS_BASE = f"{MCP_SERVER}:8082"

# Tool definitions for Ollama (subset of most useful operations)
# --- Tool registry -----------------------------------------------------------
#
# One table generates both the Ollama tool schemas and the endpoint map. They
# were previously maintained by hand as two separate structures, which is how
# the orchestrator ended up exposing 24 tools against ~85 available endpoints.
#
# Fields: (name, method, url_template, description, params, write)
#   params: {arg: (json_type, required, description)}
#   url_template may contain {placeholders}, filled from arguments and removed
#   from the query string / body.
#   write=True marks a state-changing operation — see ENABLE_WRITE_TOOLS.

Str, Int, Bool = "string", "integer", "boolean"


def _t(name, method, url, description, params=None, write=False):
    return {
        "name": name,
        "method": method,
        "url": url,
        "description": description,
        "params": params or {},
        "write": write,
    }


REGISTRY = [
    # --- vCenter: inventory -------------------------------------------------
    _t("vcenter_list_hosts", "GET", f"{VCENTER_BASE}/hosts",
       "List all ESXi hosts with status and resource usage"),
    _t("vcenter_list_vms", "GET", f"{VCENTER_BASE}/vms",
       "List all virtual machines with power state and basic info"),
    _t("vcenter_search_vms", "GET", f"{VCENTER_BASE}/vms/search",
       "Search for virtual machines by name",
       {"name": (Str, True, "VM name or partial name")}),
    _t("vcenter_vm_details", "GET", f"{VCENTER_BASE}/vm/details",
       "Detailed info for one VM: CPU, memory, disks, network, host placement",
       {"name": (Str, True, "Exact VM name")}),
    _t("vcenter_powered_off_vms", "GET", f"{VCENTER_BASE}/vms/poweredoff",
       "List virtual machines that are powered off"),
    _t("vcenter_clusters", "GET", f"{VCENTER_BASE}/clusters",
       "List vSphere clusters"),
    _t("vcenter_clusters_summary", "GET", f"{VCENTER_BASE}/clusters/summary",
       "Cluster summary including DRS/HA posture and aggregate capacity"),
    _t("vcenter_host_usage", "GET", f"{VCENTER_BASE}/hosts/usage",
       "ESXi host resource usage (CPU and memory utilization)"),

    # --- vCenter: storage ---------------------------------------------------
    _t("vcenter_datastores", "GET", f"{VCENTER_BASE}/datastores",
       "List all datastores with capacity and free space"),
    _t("vcenter_datastores_lowfree", "GET", f"{VCENTER_BASE}/datastores/lowfree",
       "Datastores below a free-space threshold — use for capacity risk questions",
       {"threshold_percent": (Int, False, "Free-space percentage threshold (default 20)")}),
    _t("vcenter_vm_storage", "GET", f"{VCENTER_BASE}/vm/storage",
       "Storage detail for one VM: disks, provisioned vs used, datastore placement",
       {"name": (Str, True, "Exact VM name")}),

    # --- vCenter: snapshots and hygiene -------------------------------------
    _t("vcenter_old_snapshots", "GET", f"{VCENTER_BASE}/snapshots/old",
       "Snapshots older than a given age that may need cleanup",
       {"days": (Int, False, "Minimum age in days (default 14)")}),
    _t("vcenter_vm_snapshots", "GET", f"{VCENTER_BASE}/vm/snapshots",
       "Full snapshot tree for one VM",
       {"name": (Str, True, "Exact VM name")}),
    _t("vcenter_vmtools_outdated", "GET", f"{VCENTER_BASE}/vmtools/outdated",
       "VMs whose VMware Tools are outdated"),
    _t("vcenter_vmtools_notrunning", "GET", f"{VCENTER_BASE}/vmtools/notrunning",
       "Powered-on VMs where VMware Tools is not running — blocks guest shutdown and backup quiescing"),

    # --- vCenter: events ----------------------------------------------------
    _t("vcenter_alarms", "GET", f"{VCENTER_BASE}/alarms",
       "Active vCenter alarms"),
    _t("vcenter_recent_tasks", "GET", f"{VCENTER_BASE}/tasks/recent",
       "Recent vCenter tasks — use to see what changed recently",
       {"limit": (Int, False, "Number of tasks (default 20)")}),
    _t("vcenter_recent_events", "GET", f"{VCENTER_BASE}/events/recent",
       "Recent vCenter events — use to investigate what happened around an incident",
       {"limit": (Int, False, "Number of events (default 20)")}),

    # --- VCF Operations: health --------------------------------------------
    _t("ops_summary", "GET", f"{OPS_BASE}/ops/summary",
       "VCF Operations environment summary: overall health, resource and alert counts"),
    _t("ops_alerts", "GET", f"{OPS_BASE}/ops/alerts",
       "Active VCF Operations alerts",
       {"activeOnly": (Bool, False, "Only active alerts (default true)"),
        "pageSize": (Int, False, "Max results")}),
    _t("ops_critical_alerts", "GET", f"{OPS_BASE}/ops/critical-alerts",
       "Critical-severity alerts only"),
    _t("ops_top_alerts", "GET", f"{OPS_BASE}/ops/top-alerts",
       "Top active alerts sorted by severity",
       {"limit": (Int, False, "Number of alerts (default 10)")}),
    _t("ops_symptoms", "GET", f"{OPS_BASE}/ops/symptoms",
       "Active symptoms detected by VCF Operations — the evidence underlying alerts"),
    _t("ops_recommendations", "GET", f"{OPS_BASE}/ops/recommendations",
       "VCF Operations optimization recommendations"),

    # --- VCF Operations: resources and metrics ------------------------------
    _t("ops_resources_search", "GET", f"{OPS_BASE}/ops/resources/search",
       "Search VCF Operations resources (VMs, hosts, clusters) by name. Returns resource IDs "
       "needed by the ops_resource_* tools",
       {"name": (Str, True, "Resource name or partial name")}),
    _t("ops_resource_details", "GET", f"{OPS_BASE}/ops/resource/{{resource_id}}",
       "Details for one VCF Operations resource by ID",
       {"resource_id": (Str, True, "Resource ID from ops_resources_search or an alert")}),
    _t("ops_resource_properties", "GET", f"{OPS_BASE}/ops/resource/{{resource_id}}/properties",
       "Configuration properties of one resource",
       {"resource_id": (Str, True, "Resource ID")}),
    _t("ops_resource_statkeys", "GET", f"{OPS_BASE}/ops/resource/{{resource_id}}/statkeys",
       "Available metric names for a resource — call before ops_resource_stats to find valid statKey values",
       {"resource_id": (Str, True, "Resource ID")}),
    _t("ops_resource_stats", "GET", f"{OPS_BASE}/ops/resource/{{resource_id}}/stats/latest",
       "Latest metric values for a resource. Use this for actual CPU/memory/latency numbers",
       {"resource_id": (Str, True, "Resource ID"),
        "statKey": (Str, False, "Specific metric key; omit for all")}),

    # --- VCF Operations: cost ----------------------------------------------
    _t("ops_cost_drivers", "GET", f"{OPS_BASE}/ops/cost-drivers",
       "Cost drivers breakdown — what is generating spend"),
    _t("ops_cost_summary", "GET", f"{OPS_BASE}/ops/cost-drivers/summary",
       "Summarized cost drivers"),
    _t("ops_cost_currency", "GET", f"{OPS_BASE}/ops/cost/currency",
       "Configured currency for cost figures"),
    _t("ops_chargeback_reports", "GET", f"{OPS_BASE}/ops/chargeback/reports",
       "Chargeback reports",
       {"name": (Str, False, "Filter by report name"),
        "status": (Str, False, "Filter by status")}),

    # --- VCF Operations: governance ----------------------------------------
    _t("ops_policies", "GET", f"{OPS_BASE}/ops/policies",
       "VCF Operations policies — alert thresholds and analysis settings"),
    _t("ops_reports", "GET", f"{OPS_BASE}/ops/reports",
       "Generated VCF Operations reports"),
    _t("ops_supermetrics", "GET", f"{OPS_BASE}/ops/supermetrics",
       "Configured supermetrics (custom derived metrics)"),

    # --- VCF Networks -------------------------------------------------------
    _t("networks_search", "GET", f"{NETWORKS_BASE}/ni/search",
       "Search VCF Networks (Network Insight) for VMs, switches, routers by name or IP",
       {"query": (Str, True, "Name or IP text"),
        "entity_type": (Str, False, "Entity type, e.g. VirtualMachine, Host, NSXSegment"),
        "size": (Int, False, "Max results")}),
    _t("networks_vms", "GET", f"{NETWORKS_BASE}/ni/vms",
       "List VM entity references only. For IPs or port groups use "
       "networks_vm_inventory instead — it returns them in one call"),
    _t("networks_vm_inventory", "GET", f"{NETWORKS_BASE}/ni/vms/inventory",
       "VMs with their IP addresses and port groups (VLANs) in a single call. "
       "Use this for any question about which VMs are on which network, or "
       "IP/port-group per VM. Optionally filter to one VLAN. Reports the total "
       "VM count and whether the list was truncated",
       {"limit": (Int, False, "Max VMs to return, default 50, max 200"),
        "vlan": (Str, False, "Only VMs on this L2 network, e.g. vlan-1000")}),
    _t("networks_vm_details", "GET", f"{NETWORKS_BASE}/ni/vms/{{vm_id}}",
       "Network detail for one VM by entity ID: IPs, segments, attached networks. "
       "Get the ID from networks_search first. Do not call this in a loop over "
       "many VMs — use networks_vm_inventory",
       {"vm_id": (Str, True, "VCF Networks entity ID, e.g. 10000:1:4378167812621755938")}),
    _t("networks_hosts", "GET", f"{NETWORKS_BASE}/ni/hosts",
       "List hosts from a network perspective"),
    _t("networks_host_details", "GET", f"{NETWORKS_BASE}/ni/hosts/{{host_id}}",
       "Network detail for one host by entity ID",
       {"host_id": (Str, True, "VCF Networks host entity ID")}),
    _t("networks_clusters", "GET", f"{NETWORKS_BASE}/ni/clusters",
       "List clusters from a network perspective"),
    _t("networks_cluster_details", "GET", f"{NETWORKS_BASE}/ni/clusters/{{cluster_id}}",
       "Network detail for one cluster by entity ID",
       {"cluster_id": (Str, True, "VCF Networks cluster entity ID")}),
    _t("networks_alerts", "GET", f"{NETWORKS_BASE}/ni/alerts",
       "Active network alerts (problems) from VCF Networks"),
    _t("networks_alert_details", "GET", f"{NETWORKS_BASE}/ni/alerts/{{problem_id}}",
       "Detail for one network alert by problem ID",
       {"problem_id": (Str, True, "Problem ID from networks_alerts")}),
    _t("networks_nsx_segments", "GET", f"{NETWORKS_BASE}/ni/entities/nsx-segments",
       "List NSX segments / logical switches",
       {"query": (Str, False, "Segment name filter"), "size": (Int, False, "Max results")}),
    _t("networks_nsx_t1", "GET", f"{NETWORKS_BASE}/ni/entities/nsx-t1",
       "List NSX Tier-1 gateways",
       {"query": (Str, False, "Tier-1 name filter"), "size": (Int, False, "Max results")}),
    _t("networks_path", "POST", f"{NETWORKS_BASE}/ni/path",
       "Firewall rules applying between two endpoints. NOTE: this does NOT return a hop-by-hop "
       "network path — Network Insight has no public topology API. Use for 'what is blocking "
       "traffic between A and B' questions, not 'what route does it take'",
       {"source": (Str, True, "Source VM name or IP"),
        "destination": (Str, True, "Destination VM name or IP"),
        "port": (Str, False, "Destination port"),
        "protocol": (Str, False, "TCP or UDP")}),
    _t("networks_flows", "GET", f"{NETWORKS_BASE}/ni/flows",
       "Traffic flows between specific VMs — actual observed conversations, bytes and ports. "
       "Give a source and/or destination VM name or IP",
       {"source": (Str, False, "Source VM name or IP"),
        "destination": (Str, False, "Destination VM name or IP"),
        "port": (Str, False, "Destination port"),
        "protocol": (Str, False, "TCP or UDP"),
        "hours": (Int, False, "Look-back window in hours (default 24)"),
        "size": (Int, False, "Max results (default 50)")}),
    _t("networks_flows_recent", "GET", f"{NETWORKS_BASE}/ni/flows/recent",
       "Recently observed traffic flows across the estate, unfiltered. Use to check whether flow "
       "data is being collected, or for a general picture of current traffic",
       {"hours": (Int, False, "Look-back window in hours (default 1)"),
        "size": (Int, False, "Max results (default 50)")}),
    _t("networks_datasources", "GET", f"{NETWORKS_BASE}/ni/data-sources/vcenters",
       "vCenter data sources registered in VCF Networks — use to confirm collection coverage"),
    _t("networks_nodes", "GET", f"{NETWORKS_BASE}/ni/infra/nodes",
       "VCF Networks infrastructure nodes (platform/collector health)"),

    # --- Write operations ---------------------------------------------------
    # Disabled unless ENABLE_WRITE_TOOLS=true. These change production state.
    _t("vcenter_vm_poweron", "POST", f"{VCENTER_BASE}/vm/poweron",
       "Power ON a virtual machine",
       {"name": (Str, True, "Exact VM name")}, write=True),
    _t("vcenter_vm_shutdown_guest", "POST", f"{VCENTER_BASE}/vm/shutdown_guest",
       "Gracefully shut down the guest OS (requires VMware Tools)",
       {"name": (Str, True, "Exact VM name")}, write=True),
    _t("vcenter_vm_reboot_guest", "POST", f"{VCENTER_BASE}/vm/reboot_guest",
       "Gracefully reboot the guest OS (requires VMware Tools)",
       {"name": (Str, True, "Exact VM name")}, write=True),
    _t("vcenter_vm_poweroff", "POST", f"{VCENTER_BASE}/vm/poweroff",
       "Hard power OFF a VM — abrupt, may cause data loss. Prefer shutdown_guest",
       {"name": (Str, True, "Exact VM name")}, write=True),
    _t("vcenter_vm_suspend", "POST", f"{VCENTER_BASE}/vm/suspend",
       "Suspend a virtual machine",
       {"name": (Str, True, "Exact VM name")}, write=True),
    _t("vcenter_vm_snapshot_create", "POST", f"{VCENTER_BASE}/vm/snapshot/create",
       "Create a snapshot of a VM",
       {"name": (Str, True, "Exact VM name"),
        "snapshot_name": (Str, True, "Snapshot name"),
        "description": (Str, False, "Snapshot description"),
        "memory": (Bool, False, "Include memory state"),
        "quiesce": (Bool, False, "Quiesce the guest filesystem")}, write=True),
    _t("vcenter_vm_snapshot_remove_all", "POST", f"{VCENTER_BASE}/vm/snapshot/remove_all",
       "Delete ALL snapshots for a VM — irreversible",
       {"name": (Str, True, "Exact VM name")}, write=True),
    _t("vcenter_vm_vmotion", "POST", f"{VCENTER_BASE}/vm/vmotion",
       "Migrate a VM to another host (vMotion)",
       {"name": (Str, True, "Exact VM name"),
        "target_host": (Str, True, "Destination host name")}, write=True),
    _t("vcenter_vm_storage_vmotion", "POST", f"{VCENTER_BASE}/vm/storage_vmotion",
       "Migrate a VM to another datastore (Storage vMotion)",
       {"name": (Str, True, "Exact VM name"),
        "target_datastore": (Str, True, "Destination datastore name")}, write=True),
    _t("vcenter_host_maintenance_enter", "POST", f"{VCENTER_BASE}/host/maintenance/enter",
       "Put an ESXi host into maintenance mode",
       {"name": (Str, True, "Host name")}, write=True),
    _t("vcenter_host_maintenance_exit", "POST", f"{VCENTER_BASE}/host/maintenance/exit",
       "Take an ESXi host out of maintenance mode",
       {"name": (Str, True, "Host name")}, write=True),
]

# networks_flows and networks_path are constrained by what Network Insight
# actually exposes: there is no public topology API, so networks_path returns
# firewall rules rather than hops. Both echo the filter/payload they used
# alongside the result, so a rejection is diagnosable rather than an opaque 4xx.

# Write tools change production state, so they are opt-in.
ENABLE_WRITE_TOOLS = os.getenv("ENABLE_WRITE_TOOLS", "false").lower() in ("1", "true", "yes")

ACTIVE_TOOLS = [t for t in REGISTRY if ENABLE_WRITE_TOOLS or not t["write"]]


def _schema(spec: dict) -> dict:
    """Build an Ollama function schema from a registry entry."""
    props, required = {}, []
    for arg, (jtype, is_required, desc) in spec["params"].items():
        props[arg] = {"type": jtype, "description": desc}
        if is_required:
            required.append(arg)
    description = spec["description"]
    if spec["write"]:
        description = f"[CHANGES STATE] {description}"
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


TOOLS = [_schema(t) for t in ACTIVE_TOOLS]
TOOL_SPECS = {t["name"]: t for t in ACTIVE_TOOLS}

# --- Scopes ------------------------------------------------------------------
#
# Every tool is offered to the model on every request, and it chooses by
# matching the question against tool descriptions. With 53 tools that is both
# ~18k prompt tokens and a lot of near-neighbours to choose between.
#
# Restricting to one system makes selection sharper and the answer cheaper, at
# the cost of questions that span systems. Membership is derived from the URL
# each tool calls, so a tool cannot end up in the wrong scope by being renamed.

SYSTEMS = {
    "vcenter": {
        "label": "vCenter",
        "base": VCENTER_BASE,
        "summary": "VMs, hosts, clusters, datastores, snapshots, alarms and power operations",
    },
    "vcf_ops": {
        "label": "VCF Operations",
        "base": OPS_BASE,
        "summary": "health, alerts, symptoms, recommendations, capacity, cost and performance metrics",
    },
    "vcf_networks": {
        "label": "VCF Networks",
        "base": NETWORKS_BASE,
        "summary": "traffic flows, VM network placement, NSX segments, firewall rules and connectivity",
    },
}


def _system_for(spec: dict) -> str:
    for key, meta in SYSTEMS.items():
        if spec["url"].startswith(meta["base"]):
            return key
    return "other"


for _spec in ACTIVE_TOOLS:
    _spec["system"] = _system_for(_spec)

TOOLS_BY_SCOPE = {"all": TOOLS}
for _key in SYSTEMS:
    TOOLS_BY_SCOPE[_key] = [_schema(t) for t in ACTIVE_TOOLS if t["system"] == _key]

SYSTEM_PROMPT = """You are an on-premises VMware infrastructure assistant. You have access to three API systems:

1. **vCenter API** — manages VMs, hosts, clusters, datastores, snapshots, alarms, and power operations
2. **VCF Operations API** — monitors health, alerts, recommendations, symptoms, cost analysis, and performance metrics
3. **VCF Networks API** — provides network topology, traffic flows, NSX segments, security policies, and connectivity

When a user asks a question:
- Use the appropriate tool(s) to gather data before answering
- Call multiple tools in parallel when the question spans multiple domains
- You may call tools again after seeing results. If a question requires
  correlating systems (e.g. find alerts, then look up the VMs or hosts they
  name), make the first calls, read the output, then make the follow-up calls
- Tool results are labelled with the tool name that produced them
- If a result is marked "_truncated", say so rather than implying it is complete
- Provide concise, actionable summaries
- If something looks unhealthy, suggest next steps
- Never guess — always check the APIs first
- Always finish with a written answer, even if the data was incomplete"""


SCOPED_PROMPT = """You are an on-premises VMware infrastructure assistant.

For this conversation you are restricted to the **{label}** system only. Your
tools cover {summary}.

When a user asks a question:
- Use the appropriate tool(s) to gather data before answering
- If answering properly needs a system you do not have tools for, say which
  system is needed and that the assistant is currently scoped to {label}.
  Do not guess at the answer or describe what the other system would show
- Tool results are labelled with the tool name that produced them
- If a result is marked "_truncated", say so rather than implying it is complete
- Provide concise, actionable summaries
- Never guess — always check the API first
- Always finish with a written answer, even if the data was incomplete"""


def prompt_for(scope: str) -> str:
    """System prompt matching the tools the model will actually be given."""
    meta = SYSTEMS.get(scope)
    if not meta:
        return SYSTEM_PROMPT
    return SCOPED_PROMPT.format(label=meta["label"], summary=meta["summary"])


async def call_api(tool_name: str, arguments: dict) -> dict:
    """Execute an API call based on the tool name and arguments."""
    spec = TOOL_SPECS.get(tool_name)
    if not spec:
        return {"error": f"Unknown tool: {tool_name}"}

    method, url = spec["method"], spec["url"]
    args = dict(arguments or {})

    # Fill {placeholders} from arguments; anything consumed here must not also
    # be sent as a query parameter or body field.
    for placeholder in re.findall(r"\{(\w+)\}", url):
        if placeholder not in args:
            return {"error": f"{tool_name} requires '{placeholder}'"}
        url = url.replace(
            "{" + placeholder + "}", urllib.parse.quote(str(args.pop(placeholder)), safe="")
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            if method == "GET":
                response = await client.get(url, params=args or None)
            else:
                response = await client.post(url, json=args or None)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            return {"error": f"API returned {e.response.status_code}: {e.response.text[:500]}"}
        except httpx.ConnectError:
            return {"error": f"Cannot connect to {url} — is the MCP server running?"}
        except Exception as e:
            return {"error": str(e)}


def summarize_tool_result(data, limit: int = TOOL_RESULT_LIMIT) -> str:
    """Serialize a tool result, trimming whole records rather than cutting JSON mid-token.

    A raw ``json.dumps(...)[:limit]`` leaves the model holding syntactically
    broken JSON with no clue it was truncated, so it either hallucinates the
    missing part or gives up. Instead drop entire list items and say so.
    """
    full = json.dumps(data, default=str)
    if len(full) <= limit:
        return full

    # Find the longest list in the payload and trim that, keeping JSON valid.
    container, key = None, None
    if isinstance(data, list):
        container = data
    elif isinstance(data, dict):
        lists = [(k, v) for k, v in data.items() if isinstance(v, list)]
        if lists:
            key, container = max(lists, key=lambda kv: len(kv[1]))

    if container:
        kept = list(container)
        while kept:
            kept.pop()
            trimmed = kept if key is None else {**data, key: kept}
            note = {
                "_truncated": True,
                "_note": (
                    f"showing {len(kept)} of {len(container)} records; "
                    "totals and counts below reflect the FULL set"
                ),
                "_total_records": len(container),
            }
            payload = (
                {"records": kept, **note} if key is None else {**trimmed, **note}
            )
            candidate = json.dumps(payload, default=str)
            if len(candidate) <= limit:
                return candidate

    # Not list-shaped (or still too big) — fall back to a truthful stub.
    return json.dumps({
        "_truncated": True,
        "_note": f"result too large to include ({len(full)} bytes)",
        "_preview": full[: max(0, limit - 200)],
    }, default=str)


# Optional inference-host telemetry (GPU utilisation, power, unified memory).
# Empty disables the feature — the orchestrator runs fine without it.
GB10_TELEMETRY_URL = os.getenv("GB10_TELEMETRY_URL", "").rstrip("/")


async def fetch_telemetry() -> dict:
    """Best-effort telemetry from the inference host.

    Never raises: this decorates an answer, and a telemetry outage must not
    turn a successful question into an error.
    """
    if not GB10_TELEMETRY_URL:
        return {}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{GB10_TELEMETRY_URL}/telemetry")
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        return {"error": str(exc)}


class Usage:
    """Accumulates Ollama token counters across the rounds of one question.

    A single question can involve several model calls (decide on tools, read
    results, decide again, answer). Reporting only the last call would badly
    understate the work done, so every round is summed.

    Ollama durations are nanoseconds.
    """

    def __init__(self):
        self.rounds = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_ns = 0
        self.eval_ns = 0
        self.load_ns = 0

    def add(self, payload: dict) -> None:
        self.rounds += 1
        self.prompt_tokens += payload.get("prompt_eval_count", 0) or 0
        self.completion_tokens += payload.get("eval_count", 0) or 0
        self.total_ns += payload.get("total_duration", 0) or 0
        self.eval_ns += payload.get("eval_duration", 0) or 0
        self.load_ns += payload.get("load_duration", 0) or 0

    def as_dict(self) -> dict:
        eval_s = self.eval_ns / 1e9
        return {
            "rounds": self.rounds,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "generation_seconds": round(eval_s, 2),
            "total_seconds": round(self.total_ns / 1e9, 2),
            "model_load_seconds": round(self.load_ns / 1e9, 2),
            # Measured over generation time only, which is the figure that
            # reflects the hardware rather than API and tool latency.
            "tokens_per_second": round(self.completion_tokens / eval_s, 1) if eval_s else None,
        }


async def chat_with_tools(user_message: str, model: str = None, conversation: list = None,
                          scope: str = "all") -> dict:
    """Send a message to Ollama with tool-calling, execute tools, return the answer.

    Returns {"answer", "usage", "tools_called"}.

    Runs an agentic loop: the model may call tools, see the results, and then
    call *more* tools based on what it found. This is what makes cross-system
    questions work ("find the critical alerts, then look up those VMs"), since
    the second lookup depends on the first one's output.
    """
    use_model = model or DEFAULT_MODEL
    tools = TOOLS_BY_SCOPE.get(scope, TOOLS)
    if conversation is None:
        conversation = [{"role": "system", "content": prompt_for(scope)}]

    conversation.append({"role": "user", "content": user_message})

    usage = Usage()
    tools_called = []

    # Large models need a longer ceiling; OLLAMA_TIMEOUT overrides
    timeout = timeout_for(use_model)

    async with httpx.AsyncClient(timeout=timeout) as client:

        async def ask(include_tools: bool) -> dict:
            body = {
                "model": use_model,
                "messages": conversation,
                "stream": False,
            }
            if include_tools:
                body["tools"] = tools
            response = await client.post(f"{OLLAMA_URL}/api/chat", json=body)
            response.raise_for_status()
            payload = response.json()
            usage.add(payload)
            return payload["message"]

        for _ in range(MAX_TOOL_ROUNDS):
            assistant_message = await ask(include_tools=True)
            conversation.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls")
            if not tool_calls:
                content = (assistant_message.get("content") or "").strip()
                if content:
                    return {
                        "answer": content,
                        "usage": usage.as_dict(),
                        "tools_called": tools_called,
                    }
                break  # empty answer with no tool calls — force a synthesis pass

            names = [tc["function"]["name"] for tc in tool_calls]
            tools_called.extend(names)

            results = await asyncio.gather(*[
                call_api(tc["function"]["name"], tc["function"].get("arguments", {}))
                for tc in tool_calls
            ])

            # Label each result with its tool name. Without this, parallel calls
            # come back as anonymous blobs the model cannot tell apart.
            for tc, result_data in zip(tool_calls, results):
                conversation.append({
                    "role": "tool",
                    "name": tc["function"]["name"],
                    "content": summarize_tool_result(result_data),
                })

        # Out of rounds, or the model stalled: ask once more with tools withheld
        # so it has to produce prose from what it has already gathered.
        conversation.append({
            "role": "user",
            "content": (
                "Answer now using the data already gathered. Do not call any more "
                "tools. If something could not be determined, say so explicitly."
            ),
        })
        final = await ask(include_tools=False)
        answer = (final.get("content") or "").strip() or (
            "The model returned an empty response. Try rephrasing, or use a "
            "model with stronger tool-calling support."
        )
        return {
            "answer": answer,
            "usage": usage.as_dict(),
            "tools_called": tools_called,
        }


# --- API Endpoints ---

class ChatRequest(BaseModel):
    message: str
    model: str = None  # Optional model override
    scope: str = "all"  # "all", or one of SYSTEMS: vcenter / vcf_ops / vcf_networks

class ChatResponse(BaseModel):
    answer: str
    model: str
    usage: dict = {}
    tools_called: list = []
    telemetry: dict = {}


@app.get("/scopes")
async def list_scopes():
    """Tool scopes the UI can offer, with how many tools each covers."""
    out = [{
        "id": "all",
        "label": "All systems",
        "summary": "every tool; needed for questions that correlate across systems",
        "tool_count": len(TOOLS),
    }]
    for key, meta in SYSTEMS.items():
        out.append({
            "id": key,
            "label": meta["label"],
            "summary": meta["summary"],
            "tool_count": len(TOOLS_BY_SCOPE.get(key, [])),
        })
    return out


@app.get("/config")
async def config():
    """Backend configuration, with no liveness probing.

    Separate from /health so callers that only need to display which backends
    are configured return instantly, instead of waiting on probe timeouts.
    """
    return {
        "ollama_url": OLLAMA_URL,
        "mcp_server": MCP_SERVER,
        "default_model": DEFAULT_MODEL,
        "max_tool_rounds": MAX_TOOL_ROUNDS,
        "tool_count": len(TOOLS),
        "write_tools_enabled": ENABLE_WRITE_TOOLS,
        "telemetry_url": GB10_TELEMETRY_URL or None,
    }


async def _probe_ollama() -> dict:
    """Check the inference backend: reachable, model present, model resident."""
    info = {"url": OLLAMA_URL, "reachable": False}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            tags = await client.get(f"{OLLAMA_URL}/api/tags")
            tags.raise_for_status()
            info["reachable"] = True
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)

            installed = [m.get("model") for m in tags.json().get("models", [])]
            info["default_model_installed"] = DEFAULT_MODEL in installed

            # A resident model answers immediately; a cold one pays a load cost
            ps = await client.get(f"{OLLAMA_URL}/api/ps")
            if ps.status_code == 200:
                info["models_resident"] = [m.get("model") for m in ps.json().get("models", [])]
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


async def _probe_api(name: str, base: str, health_path: str) -> dict:
    """Check one backing API can actually serve data.

    Probing /docs only proved uvicorn was listening: the orchestrator once
    reported "ok" while every vCenter call returned 500. Ask the API's own
    health route and carry its verdict through.
    """
    info = {"name": name, "url": base, "reachable": False}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}{health_path}")
            info["status_code"] = resp.status_code
            info["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            body = {}
            try:
                body = resp.json()
            except Exception:
                pass
            reported = body.get("status") if isinstance(body, dict) else None
            info["reachable"] = resp.status_code < 500 and reported != "unavailable"
            if reported:
                info["backend_status"] = reported
            if isinstance(body, dict) and body.get("error"):
                info["error"] = body["error"]
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


async def _installed_models() -> set[str]:
    """Model names the configured Ollama host actually has.

    Empty set means we could not ask, which is treated as "don't block".
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code == 200:
                return {m.get("model", "") for m in resp.json().get("models", [])}
    except Exception:
        pass
    return set()


@app.get("/health")
async def health():
    """Report whether inference and the backing APIs are actually reachable.

    Always returns 200 so a probe can read the detail; use the `status`
    field rather than the HTTP code to decide if the system is usable.
    """
    ollama, vcenter, ops, networks = await asyncio.gather(
        _probe_ollama(),
        _probe_api("vcenter", VCENTER_BASE, "/health"),
        _probe_api("vcf_ops", OPS_BASE, "/ops/health"),
        _probe_api("vcf_networks", NETWORKS_BASE, "/ni/health"),
    )

    apis = [vcenter, ops, networks]
    if not ollama["reachable"]:
        status = "unavailable"  # no inference, nothing works
    elif not all(a["reachable"] for a in apis):
        status = "degraded"     # can answer, but not from live data
    else:
        status = "ok"

    return {
        "status": status,
        "default_model": DEFAULT_MODEL,
        "available_models": list(AVAILABLE_MODELS.keys()),
        "mcp_server": MCP_SERVER,
        "inference": ollama,
        "apis": apis,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Ask a question about your VMware infrastructure."""
    use_model = request.model or DEFAULT_MODEL
    # The curated list describes models; it does not decide which exist. Ask the
    # inference host, so the same code works on the GB10 and on a laptop without
    # editing a table every time a model is pulled.
    installed = await _installed_models()
    if installed and use_model not in installed:
        raise HTTPException(
            status_code=400,
            detail=f"Model {use_model} is not installed on {OLLAMA_URL}. "
                   f"Installed: {sorted(installed)}",
        )
    # Fall back silently and a typo'd scope quietly gets all 53 tools, which
    # looks like it worked.
    if request.scope not in TOOLS_BY_SCOPE:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope: {request.scope}. Valid: {sorted(TOOLS_BY_SCOPE)}",
        )
    try:
        result = await chat_with_tools(request.message, model=use_model, scope=request.scope)
        # Telemetry is best-effort decoration; a dead exporter must not fail a
        # question that was answered successfully.
        return ChatResponse(
            answer=result["answer"],
            model=use_model,
            usage=result["usage"],
            tools_called=result["tools_called"],
            telemetry=await fetch_telemetry(),
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail=f"Cannot reach Ollama at {OLLAMA_URL} — is it running and reachable?")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/telemetry")
async def telemetry():
    """Live inference-host telemetry, for dashboards that poll independently."""
    if not GB10_TELEMETRY_URL:
        return {"enabled": False, "reason": "GB10_TELEMETRY_URL is not set"}
    return {"enabled": True, **await fetch_telemetry()}


@app.get("/models")
async def list_models():
    """List models, marking which are installed on the configured host."""
    installed = await _installed_models()
    out = {k: dict(v) for k, v in AVAILABLE_MODELS.items()}
    for name in installed:
        out.setdefault(name, {"name": name, "description": "Installed locally"})
    for name, meta in out.items():
        meta["installed"] = (name in installed) if installed else None
    return out


@app.get("/tools")
async def list_tools():
    """List all available tools the LLM can use."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "method": t["method"],
            "write": t["write"],
        }
        for t in ACTIVE_TOOLS
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)
