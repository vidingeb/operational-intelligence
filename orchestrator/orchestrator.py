"""
On-prem AI Orchestrator — routes natural-language questions to vCenter,
VCF Operations, and VCF Networks APIs via an Ollama LLM.

Inference and data can live in different places. By default everything is
local, matching the original single-site deployment: the orchestrator runs on
the LLM VM and calls APIs on the MCP server.

Set OLLAMA_URL to point inference somewhere else — for example a DGX Spark
GB10 reachable over a tailnet — and only prompts and tool results leave the
site. vCenter credentials and the API surface stay put.

Addresses in this file use the RFC 5737 documentation range (192.0.2.0/24).
They are placeholders — set MCP_SERVER and OLLAMA_URL for your own hosts.

Environment:
    OLLAMA_URL      Ollama endpoint       (default http://localhost:11434)
    MCP_SERVER      API host base URL     (default http://192.0.2.140)
    DEFAULT_MODEL   Model to use          (default llama3.1:8b)
    OLLAMA_TIMEOUT  Seconds, overrides the per-model default
    MAX_TOOL_ROUNDS Agentic tool-calling rounds  (default 8)
    ENABLE_WRITE_TOOLS      Expose state-changing tools   (default false)
    WRITE_REQUIRE_CONFIRM   Writes must be confirmed      (default true)
    AUDIT_LOG               Path for the write audit trail
"""

import os
import re
import json
import urllib.parse
import time
import secrets
from contextlib import asynccontextmanager
import httpx
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import store
import schedule_times



@asynccontextmanager
async def lifespan(_app):
    """Open the state database and start the schedule runner.

    A lifespan handler rather than @app.on_event: the latter is deprecated, and
    a scheduler that silently stops starting after a FastAPI upgrade is exactly
    the kind of quiet failure this system keeps running into.
    """
    store.init_db()
    task = asyncio.create_task(scheduler_loop()) if SCHEDULER_ENABLED else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="On-Prem AI Orchestrator", version="1.2", lifespan=lifespan)

# Configuration — env-overridable so the same code runs single- or split-site
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MCP_SERVER = os.getenv("MCP_SERVER", "http://192.0.2.140").rstrip("/")
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
# question whose second lookup depends on the first one's answer. Diagnosis
# needs more headroom than a lookup: gather, correlate, then check the thing
# the correlation pointed at.
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "8"))

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
LOGS_BASE = f"{MCP_SERVER}:8083"
VEEAM_BASE = f"{MCP_SERVER}:8084"

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


def _t(name, method, url, description, params=None, write=False, local=None):
    return {
        "name": name,
        "method": method,
        "url": url,
        "description": description,
        "params": params or {},
        "write": write,
        "local": local,
    }


REGISTRY = [
    # --- vCenter: inventory -------------------------------------------------
    _t("vcenter_about", "GET", f"{VCENTER_BASE}/about",
       "vCenter Server's own version, build, API version and licence edition. "
       "Use for 'what version is vCenter' or 'what are we running'. This is the "
       "vCenter appliance itself, not the ESXi hosts — their versions come from "
       "vcenter_list_hosts and can differ"),
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
    _t("vcenter_vm_versions", "GET", f"{VCENTER_BASE}/vms/versions",
       "Virtual hardware version (vmx-NN) and VMware Tools version/status for "
       "every VM, grouped by version with counts and sample names. Use for "
       "'what hardware version are the VMs on', 'are tools up to date', and "
       "upgrade or lifecycle planning. Compares against the newest hardware "
       "version actually present, not a fixed maximum",
       {"limit": (Int, False, "Max per-VM rows returned (default 500)")}),

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
    _t("networks_version", "GET", f"{NETWORKS_BASE}/ni/version",
       "Version of VCF Operations for Networks (Network Insight) itself. For a "
       "question spanning more than one product, prefer estate_versions"),
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
    _t("networks_flow_inventory", "GET", f"{NETWORKS_BASE}/ni/flows/inventory",
       "PREFERRED for any question about observed traffic: which flows are north-south or "
       "east-west, which VMs are talking, to what, on which ports, and whether the firewall "
       "allowed it. Resolves every flow server-side in one call and returns a traffic_type "
       "breakdown. Use this instead of networks_flows_recent whenever the question is about "
       "the content of the traffic rather than merely whether collection is working",
       {"hours": (Int, False, "Look-back window in hours (default 1)"),
        "limit": (Int, False, "Max flows to resolve (default 100)"),
        "traffic_type": (Str, False, "Filter, e.g. north_south or east_west"),
        "vm": (Str, False, "Only flows where this VM is source or destination")}),
    _t("networks_flows_recent", "GET", f"{NETWORKS_BASE}/ni/flows/recent",
       "Raw flow identifiers only — returns entity IDs with no IPs, VM names or ports. "
       "Use ONLY to confirm that flow collection is running. It cannot answer any question "
       "about who is talking to whom; for that use networks_flow_inventory",
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

    # --- VCF Operations for Logs -------------------------------------------
    _t("logs_search", "GET", f"{LOGS_BASE}/logs/search",
       "Free-text search of infrastructure logs. Use when a question needs evidence of "
       "what actually happened — errors, failures, restarts, specific messages",
       {"contains": (Str, False, "Substring to match in the log text"),
        "hours": (Int, False, "Look-back window in hours (default 1)"),
        "limit": (Int, False, "Max events (default 100)")}),
    _t("logs_errors", "GET", f"{LOGS_BASE}/logs/errors",
       "Recent error-level log activity across the estate. Use for 'what is failing' "
       "and as corroboration when an alarm needs explaining",
       {"hours": (Int, False, "Look-back window in hours (default 1)"),
        "limit": (Int, False, "Max events (default 100)")}),
    _t("logs_for_object", "GET", f"{LOGS_BASE}/logs/for/{{name}}",
       "Log events mentioning a named VM, host, datastore or service. Use to find out "
       "WHY something failed after an alarm or alert has said THAT it failed",
       {"name": (Str, True, "VM, host or object name"),
        "hours": (Int, False, "Look-back window in hours (default 24)"),
        "limit": (Int, False, "Max events (default 100)")}),
    _t("logs_version", "GET", f"{LOGS_BASE}/logs/version",
       "Version of VCF Operations for Logs itself. For a question spanning more "
       "than one product, prefer estate_versions"),

    # --- Veeam Backup & Replication ----------------------------------------
    _t("veeam_protection", "GET", f"{VEEAM_BASE}/veeam/protection/{{vm_name}}",
       "Whether a specific VM is actually backed up, and how old its newest restore "
       "point is. Derived from restore points, not job status — a job can report "
       "success while skipping a VM. Use before any destructive change to a VM",
       {"vm_name": (Str, True, "Exact VM name"),
        "stale_after_hours": (Int, False, "Age at which a backup counts as stale (default 48)")}),
    _t("veeam_failed_jobs", "GET", f"{VEEAM_BASE}/veeam/sessions",
       "Recent Veeam job runs and their outcome. Set failed_only=true for backup failures",
       {"hours": (Int, False, "Look-back window in hours (default 24)"),
        "failed_only": (Bool, False, "Only sessions that did not succeed"),
        "limit": (Int, False, "Max sessions (default 200)")}),
    _t("veeam_unprotected", "GET", f"{VEEAM_BASE}/veeam/unprotected",
       "Objects known to Veeam that have no restore points. Note this cannot prove the "
       "estate is fully protected — a VM never added to a job does not appear at all",
       {"limit": (Int, False, "Max objects (default 200)")}),
    _t("veeam_protected", "GET", f"{VEEAM_BASE}/veeam/protected",
       "Every object Veeam knows about and its newest restore point. This is "
       "Veeam's roster, not the estate — prefer backup_coverage, which compares "
       "it against the vCenter inventory"),
    _t("veeam_jobs", "GET", f"{VEEAM_BASE}/veeam/jobs",
       "Configured Veeam backup jobs",
       {"limit": (Int, False, "Max jobs (default 100)")}),
    _t("veeam_version", "GET", f"{VEEAM_BASE}/veeam/version",
       "Veeam Backup & Replication build and REST API version. For a question "
       "spanning more than one product, prefer estate_versions"),

    # --- Triage ------------------------------------------------------------
    # Composite, cross-system, executed in-process. A senior engineer does not
    # answer "what is wrong with adc01" with one lookup; they gather state,
    # alarms, snapshots, storage and traffic and then correlate. Expressed as
    # single tools because doing it as eight separate calls exhausted the
    # round budget before any correlation happened.
    _t("triage_vm", "LOCAL", "local://triage/vm",
       "START HERE for any question about a VM being slow, broken, degraded, "
       "unreachable or 'having problems', and before proposing any change to a VM. "
       "Gathers configuration, power state, VMware Tools, snapshots, storage, "
       "matching vCenter alarms, VCF Operations alerts and recent network flows "
       "in one call, across all three systems",
       {"name": (Str, True, "Exact VM name")}, local="triage_vm"),
    _t("triage_host", "LOCAL", "local://triage/host",
       "START HERE for any question about an ESXi host being unhealthy, overloaded "
       "or degraded, and ALWAYS before putting a host into maintenance mode. "
       "Gathers host status and utilisation, its cluster, matching alarms, "
       "VCF Operations alerts and low-free datastores in one call",
       {"name": (Str, True, "Exact host name")}, local="triage_host"),
    _t("triage_estate", "LOCAL", "local://triage/estate",
       "Estate-wide health sweep across all five systems: critical vCenter alarms, "
       "VCF Operations alerts, network alerts, low-free datastores, old snapshots, "
       "VMs with VMware Tools not running, error-level logs and failed backups. "
       "Use for 'how is everything', 'any problems', morning-check and reporting "
       "questions. Returns a count plus a sample per section; set full=true only "
       "when the complete lists are genuinely needed, as it is much larger",
       {"full": ("boolean", False,
                 "Return every record instead of a sample per section.")},
       local="triage_estate"),
    _t("backup_coverage", "LOCAL", "local://triage/backup-coverage",
       "Which VMs have no restore point, by comparing the full vCenter inventory "
       "against everything Veeam protects. Use for 'are we backed up', 'which VMs "
       "are unprotected', 'do we have a restore point for X' and any backup "
       "coverage question. Veeam alone cannot answer this: a VM never added to a "
       "job is absent from its records entirely, so 'no unprotected objects' only "
       "ever describes the objects already in a job",
       {"stale_after_hours": (Int, False,
                              "Age at which a restore point counts as stale (default 48)")},
       local="backup_coverage"),
    _t("estate_versions", "LOCAL", "local://versions/estate",
       "Software versions across the whole estate in one call: vCenter, every "
       "ESXi host, VCF Operations for Logs, VCF Operations for Networks and "
       "Veeam. Use for 'what versions are we running', 'what software is in the "
       "cluster' or any question spanning more than one product. Also reports "
       "which systems have no version source (NSX is not integrated), so the "
       "answer can say what was not checked",
       local="estate_versions"),
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
    "logs": {
        "label": "Logs",
        "base": LOGS_BASE,
        "summary": "infrastructure log search and error events — evidence of what actually happened",
    },
    "backup": {
        "label": "Veeam Backup",
        "base": VEEAM_BASE,
        "summary": "backup jobs, job outcomes, restore points and which VMs are actually protected",
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

ENGINEER_RULES = """
You are a senior VMware infrastructure engineer, not a search interface. Work
the way an experienced engineer does.

**Evidence before conclusions**
- Never guess, and never answer infrastructure questions from general VMware
  knowledge. Check the APIs. If you did not read it from a tool result, you do
  not know it.
- If a tool returns an error, or a result marked "_truncated", or a section
  containing "error", say so plainly. An unavailable check is not a passed check.
- Absence of an alarm is not evidence of health; it is evidence that nothing
  raised an alarm. Say which is which.
- Quote the specific object names, values and thresholds you based a conclusion
  on, so the operator can verify you.

**Diagnose, do not lookup**
- For "X is slow / broken / degraded / having problems", start with triage_vm or
  triage_host. One call gathers state, alarms, alerts, storage and traffic.
- Then correlate. A VM problem is usually explained by its host, its datastore,
  its snapshots or its network, not by the VM record alone. Follow the evidence
  into a second call when the first one points somewhere.
- Distinguish symptom from cause. "Datastore 8% free" is a cause; "VM is slow"
  is a symptom. Report the causal chain, not a list of facts.
- Rank findings by operational severity, not by how loudly a system reports
  them. A red banner is not automatically the worst thing on the list.
  Roughly, worst first:
  1. Data loss or an inability to recover it — failed or missing backups, no
     recent restore point, a datastore at capacity, failing storage.
  2. Service down or about to be — hosts unresponsive or isolated, VMs
     powered off unexpectedly, a cluster with no failover capacity left.
  3. Degradation — resource contention, latency, dropped traffic, hardware
     sensors reporting faults.
  4. Hygiene and administrative — old snapshots, VMware Tools out of date,
     licensing and inventory warnings.
  Licensing is category 4 even when vCenter colours it red. A backup that has
  been failing for months outranks it, because one is paperwork and the other
  means the data is not recoverable.
- Age matters as much as severity. A red alarm dated months ago is a
  longstanding failure that nobody acted on, not old news — say how long it
  has been in that state.
- When two systems report the same underlying problem, say so explicitly and
  treat it as one finding with corroboration, not two separate items.

**Changing state**
- Every state-changing tool returns AWAITING_CONFIRMATION and changes nothing.
  That is expected. Report the proposal and stop.
- Never tell the operator an action is done, running, or scheduled unless you
  have seen a result with "executed": true.
- Before proposing a change, gather evidence justifying it, and say what it will
  affect. Before maintenance mode, check the cluster can absorb the host's VMs.
- Prefer the least destructive option that solves the problem: guest shutdown
  over hard power off, vMotion over downtime. If the operator asks for something
  destructive, propose it, but state the risk in the same breath.
- Recommend one clear next action rather than listing every possibility.

**Reporting**
- Be concise and specific. An operator wants "esx03: 94% memory, 3 VMs
  ballooning" not a paragraph of narration.
- Say what you checked, so the boundaries of the answer are visible.
- Output is displayed in a pane that renders Markdown tables as real tables
  with a CSV download button. Use a Markdown table whenever you are reporting
  more than about three items that share the same fields — one row per object,
  a header row, and a separator row. Do not emit HTML such as <br> in prose; it
  is stripped. Prose, headings and "- " bullets are fine for everything else.
- The pane renders Mermaid diagrams from a ```mermaid fenced block. Use one for
  topology, dependencies and design questions, where a picture carries what a
  table cannot. Two syntax rules matter, because breaking either fails the
  whole diagram rather than one line, and the operator sees an error instead of
  a picture:
  1. A %% comment must be on a line of its own. Never append one to a
     statement: "a --> b %% note" is a parse error, not an ignored comment.
  2. For a line break inside a label use <br/>, which is preserved inside a
     fence. Do not rely on a backslash-n escape.
  Diagram the estate as you actually measured it. A diagram is read as fact and
  invites less scrutiny than prose, so never draw a component you have not
  confirmed, and label anything unverified as such.
- Put every row you are reporting in the table. Never write "the remaining N
  follow the same pattern", "omitted for brevity", or "the full list is in the
  raw output": you have not checked that they do, the operator cannot see the
  raw output, and it hides the exceptions that make the table worth reading.
  If there are genuinely too many rows, show them anyway; if you must stop,
  never label a partial table as complete. Writing "shows every VM" above a
  table that omits rows is worse than omitting them openly.
- A sample is not a complete set. If a result carries "showing" or
  "more_available", say so rather than presenting it as everything there is.
- A clean result is only as good as the population it was computed over.
  Before reporting that nothing is wrong, check what was actually examined and
  compare it against the full inventory. "0 objects without restore points" out
  of 11 objects, in an estate of 63 VMs, is not a backup pass — the finding is
  the 52 that no backup system has ever heard of. Lead with the gap, not with
  the zero. For backup questions this join is done for you: use backup_coverage,
  never veeam_unprotected alone.
- Never offer to run a query you have no tool for. Saying "let me know and I
  can run a targeted query" when no such tool exists sounds helpful and is
  untrue. State the limit and stop: "NSX is not integrated, so I cannot read
  its version."
- Always finish with a written answer, even when the data was incomplete."""

SYSTEM_PROMPT = """You are an on-premises VMware infrastructure assistant with
direct API access to a live production estate.

Systems available to you:

1. **vCenter** — VMs, hosts, clusters, datastores, snapshots, alarms, tasks,
   events, and power/migration/maintenance operations
2. **VCF Operations** — health, alerts, symptoms, recommendations, capacity,
   cost and performance metrics
3. **VCF Networks** — traffic flows, VM network placement, NSX segments,
   firewall rules and connectivity
4. **Triage tools** — cross-system evidence gathering; prefer these for any
   question about something being wrong

Tool results are labelled with the tool that produced them. You may call tools
in parallel, and you may call more tools after reading results — use this to
correlate across systems.
""" + ENGINEER_RULES


SCOPED_PROMPT = """You are an on-premises VMware infrastructure assistant with
direct API access to a live production estate.

For this conversation you are restricted to the **{label}** system only. Your
tools cover {summary}.

If answering properly needs a system you do not have tools for, say which system
is needed and that the assistant is currently scoped to {label}. Do not guess at
the answer, and do not describe what the other system would have shown.

Cross-system triage tools are not available in this scope.
""" + ENGINEER_RULES


def prompt_for(scope: str) -> str:
    """System prompt matching the tools the model will actually be given."""
    meta = SYSTEMS.get(scope)
    if not meta:
        return SYSTEM_PROMPT
    return SCOPED_PROMPT.format(label=meta["label"], summary=meta["summary"])


# --- Write safety ------------------------------------------------------------
#
# The write tools were finished long before anything was safe to run them: an
# unconfirmed vcenter_vm_poweroff on a misparsed name hard-stops a production
# VM, and snapshot_remove_all is irreversible. So a write tool call does not
# execute. It reads the current state, returns a proposal describing what would
# change, and waits for the operator to confirm the token.
#
# Every executed write is appended to an audit log, and the state is re-read
# afterwards: an API reporting success is not evidence that anything happened.

WRITE_REQUIRE_CONFIRM = os.getenv("WRITE_REQUIRE_CONFIRM", "true").lower() in ("1", "true", "yes")
AUDIT_LOG = os.getenv("AUDIT_LOG", os.path.join(os.path.dirname(__file__), "audit.log"))
PENDING_TTL = int(os.getenv("PENDING_TTL", "600"))

# Operations with no undo. Called out separately in the proposal so the
# operator is told which of these cannot be walked back.
IRREVERSIBLE = {
    "vcenter_vm_poweroff": "Pulls virtual power immediately. The guest is not asked to flush "
                           "anything to disk, so in-flight writes can be lost.",
    "vcenter_vm_snapshot_remove_all": "Deletes every snapshot. There is no undo and no recovery "
                                      "point afterwards.",
    "vcenter_vm_reset": "Equivalent to the reset button. Same data-loss risk as a hard power off.",
    "vcenter_host_reboot": "Reboots the host. Any VM still running on it goes down with it.",
    "vcenter_host_shutdown": "Powers the host off. It will need physical or out-of-band access "
                             "to come back.",
}

# Read tool used to describe what a write would affect, and to verify the
# result afterwards. The argument is passed straight through.
IMPACT_PROBE = {
    "vcenter_vm_poweron": "vcenter_vm_details",
    "vcenter_vm_poweroff": "vcenter_vm_details",
    "vcenter_vm_shutdown_guest": "vcenter_vm_details",
    "vcenter_vm_reboot_guest": "vcenter_vm_details",
    "vcenter_vm_suspend": "vcenter_vm_details",
    "vcenter_vm_reset": "vcenter_vm_details",
    "vcenter_vm_vmotion": "vcenter_vm_details",
    "vcenter_vm_storage_vmotion": "vcenter_vm_storage",
    "vcenter_vm_snapshot_create": "vcenter_vm_snapshots",
    "vcenter_vm_snapshot_remove_all": "vcenter_vm_snapshots",
    "vcenter_host_maintenance_enter": "vcenter_host_usage",
    "vcenter_host_maintenance_exit": "vcenter_host_usage",
}

PENDING: dict = {}


def _audit(event: dict) -> None:
    """Append one line per write attempt. Best effort — never break the call."""
    event["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        with open(AUDIT_LOG, "a") as handle:
            handle.write(json.dumps(event, default=str) + "\n")
    except OSError as exc:
        print(f"audit log unwritable ({AUDIT_LOG}): {exc}")


def _expire_pending() -> None:
    cutoff = time.time() - PENDING_TTL
    for token in [t for t, p in PENDING.items() if p["proposed_at"] < cutoff]:
        PENDING.pop(token, None)


async def _probe_state(tool_name: str, arguments: dict):
    """Current state of whatever a write is about to change."""
    probe = IMPACT_PROBE.get(tool_name)
    if not probe or probe not in TOOL_SPECS:
        return None
    name = (arguments or {}).get("name")
    try:
        return await call_api(probe, {"name": name} if name else {})
    except Exception as exc:
        return {"error": f"could not read current state: {exc}"}


async def propose_write(tool_name: str, arguments: dict) -> dict:
    """Describe a state change and hand back a token instead of doing it."""
    _expire_pending()
    spec = TOOL_SPECS[tool_name]
    state_before = await _probe_state(tool_name, arguments)

    token = secrets.token_urlsafe(6)
    PENDING[token] = {
        "tool": tool_name,
        "arguments": arguments,
        "proposed_at": time.time(),
        "state_before": state_before,
    }

    _audit({"event": "proposed", "token": token, "tool": tool_name, "arguments": arguments})

    return {
        "status": "AWAITING_CONFIRMATION",
        "executed": False,
        "confirmation_token": token,
        "action": spec["description"].replace("[CHANGES STATE] ", ""),
        "tool": tool_name,
        "arguments": arguments,
        "irreversible": tool_name in IRREVERSIBLE,
        "warning": IRREVERSIBLE.get(tool_name),
        "current_state": state_before,
        "expires_in_seconds": PENDING_TTL,
        "instruction": (
            "NOTHING HAS BEEN CHANGED. Tell the operator exactly what this would do, "
            "name the object it affects, quote anything in current_state that makes it "
            "risky, and state that it needs confirmation. Do not say the action is done, "
            "in progress, or scheduled. Do not call this tool again."
        ),
    }


async def execute_pending(token: str) -> dict:
    """Run a previously proposed write, then re-read the state it changed."""
    _expire_pending()
    pending = PENDING.pop(token, None)
    if not pending:
        raise HTTPException(
            status_code=404,
            detail="Unknown or expired confirmation token. Ask for the action again.",
        )

    tool_name, arguments = pending["tool"], pending["arguments"]
    result = await call_api(tool_name, arguments, confirmed=True)
    failed = isinstance(result, dict) and result.get("error")

    # An API reporting success is not evidence the state changed, so look.
    state_after = await _probe_state(tool_name, arguments)

    _audit({
        "event": "executed",
        "token": token,
        "tool": tool_name,
        "arguments": arguments,
        "error": result.get("error") if isinstance(result, dict) else None,
        "state_before": pending["state_before"],
        "state_after": state_after,
    })

    return {
        "status": "FAILED" if failed else "EXECUTED",
        "executed": not failed,
        "tool": tool_name,
        "arguments": arguments,
        "result": result,
        "state_before": pending["state_before"],
        "state_after": state_after,
    }



async def call_api(tool_name: str, arguments: dict, confirmed: bool = False) -> dict:
    """Execute an API call based on the tool name and arguments.

    State-changing tools do not execute here by default. They return a
    proposal for the operator to confirm — see propose_write.
    """
    spec = TOOL_SPECS.get(tool_name)
    if not spec:
        return {"error": f"Unknown tool: {tool_name}"}

    if spec.get("local"):
        handler = LOCAL_HANDLERS.get(spec["local"])
        if not handler:
            return {"error": f"{tool_name} has no handler"}
        return await handler(**(arguments or {}))

    if spec["write"] and WRITE_REQUIRE_CONFIRM and not confirmed:
        return await propose_write(tool_name, arguments or {})

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


# --- Triage ------------------------------------------------------------------
#
# Cross-system evidence gathering. Each of these replaces a sequence a senior
# engineer would run by hand, and would otherwise cost one tool round each —
# more rounds than the loop allows, so the correlation never happened.
#
# Failures are reported per section rather than aborting: a triage that says
# "VCF Operations unreachable" is useful, one that returns a single error is not.

def _mentions(blob, needle: str) -> bool:
    """Does this record refer to the named object anywhere in its fields?"""
    return needle.lower() in json.dumps(blob, default=str).lower()


def _filter_mentions(data, needle: str, keys=("alerts", "alarms", "results", "items")):
    """Pull the list out of a payload and keep entries naming the object."""
    if isinstance(data, dict) and data.get("error"):
        return data
    rows = data if isinstance(data, list) else None
    if rows is None and isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    if rows is None:
        return data
    return [row for row in rows if _mentions(row, needle)]


async def _gather(sections: dict) -> dict:
    """Run named tool calls concurrently, keeping each one's failure local."""
    names = list(sections)
    results = await asyncio.gather(
        *[call_api(tool, args) for tool, args in sections.values()],
        return_exceptions=True,
    )
    out = {}
    for name, result in zip(names, results):
        out[name] = {"error": str(result)} if isinstance(result, Exception) else result
    return out


async def triage_vm(name: str) -> dict:
    """Everything relevant to one VM, from all five systems, in one call."""
    data = await _gather({
        "vm": ("vcenter_vm_details", {"name": name}),
        "snapshots": ("vcenter_vm_snapshots", {"name": name}),
        "storage": ("vcenter_vm_storage", {"name": name}),
        "vcenter_alarms": ("vcenter_alarms", {}),
        "ops_alerts": ("ops_alerts", {"activeOnly": True, "pageSize": 200}),
        "recent_flows": ("networks_flow_inventory", {"vm": name, "hours": 24, "limit": 200}),
        "logs": ("logs_for_object", {"name": name, "hours": 24, "limit": 50}),
        "backup": ("veeam_protection", {"vm_name": name}),
    })

    data["vcenter_alarms"] = _filter_mentions(data["vcenter_alarms"], name)
    data["ops_alerts"] = _filter_mentions(data["ops_alerts"], name)

    flows = data.get("recent_flows")
    if isinstance(flows, dict) and not flows.get("error"):
        data["recent_flows"] = {
            "flow_count": flows.get("flow_count"),
            "traffic_type_breakdown": flows.get("traffic_type_breakdown"),
            "flows": (flows.get("flows") or [])[:15],
        }

    # 50 full log events is the largest single contributor here, and the model
    # reads the first handful to spot a pattern rather than all of them.
    data["logs"] = _condense(data.get("logs"), keep=10)

    return {
        "triage_target": name,
        "target_type": "VirtualMachine",
        "systems_consulted": ["vCenter", "VCF Operations", "VCF Networks", "Logs", "Veeam"],
        "guidance": (
            "Alarms and alerts were matched by name, so an empty list means none "
            "mentioned this VM, not that the estate is healthy. Any section "
            "containing 'error' was not collected — say so rather than treating "
            "it as a clean result. Logs explain why something failed once alarms "
            "have established that it did. Check 'backup' before recommending "
            "anything destructive."
        ),
        **data,
    }


async def triage_host(name: str) -> dict:
    """Everything relevant to one ESXi host, including what it would displace."""
    data = await _gather({
        "hosts": ("vcenter_list_hosts", {}),
        "host_usage": ("vcenter_host_usage", {}),
        "clusters": ("vcenter_clusters_summary", {}),
        "vcenter_alarms": ("vcenter_alarms", {}),
        "ops_alerts": ("ops_alerts", {"activeOnly": True, "pageSize": 200}),
        "datastores_low": ("vcenter_datastores_lowfree", {"threshold_percent": 20}),
        "network_alerts": ("networks_alerts", {}),
        "logs": ("logs_for_object", {"name": name, "hours": 24, "limit": 50}),
    })

    for section in ("hosts", "host_usage", "vcenter_alarms", "ops_alerts", "network_alerts"):
        data[section] = _filter_mentions(data[section], name)

    data["logs"] = _condense(data.get("logs"), keep=10)

    return {
        "triage_target": name,
        "target_type": "HostSystem",
        "systems_consulted": ["vCenter", "VCF Operations", "VCF Networks", "Logs"],
        "guidance": (
            "Before proposing maintenance mode, check cluster capacity in 'clusters': "
            "the VMs on this host must fit elsewhere. Sections containing 'error' "
            "were not collected."
        ),
        **data,
    }


# Keys that hold the actual findings in a wrapper response. Checked in order,
# so a payload with both "results" and "events" condenses on the meaningful one.
_RESULT_KEYS = ("alarms", "alerts", "events", "datastores", "snapshots",
                "vms", "hosts", "clusters", "jobs", "sessions", "results")


def _condense(section: Any, keep: int = 5) -> Any:
    """Reduce one triage section to a count plus a sample.

    An estate sweep gathers ten sections from five systems, and passing all of
    them through whole cost ~49,500 input tokens for a single question. Most of
    that is detail the model never reads: it needs to know that eight
    datastores are low and which are worst, not the full record for each.

    The count is always the true total, and "showing" says how much of it is
    here, so a sample is never mistaken for the whole set. Sections that
    failed are passed through untouched — a failure must stay visible.
    """
    if isinstance(section, dict):
        if section.get("error"):
            return section
        for key in _RESULT_KEYS:
            value = section.get(key)
            if isinstance(value, list):
                condensed = {k: v for k, v in section.items()
                             if not isinstance(v, (list, dict))}
                condensed["count"] = len(value)
                condensed[key] = value[:keep]
                if len(value) > keep:
                    condensed["showing"] = f"{keep} of {len(value)}"
                    condensed["more_available"] = (
                        "Call the specific tool for this area to see the rest. "
                        "Do not describe this sample as the complete list."
                    )
                return condensed
        return section

    if isinstance(section, list):
        if len(section) <= keep:
            return section
        return {
            "count": len(section),
            "showing": f"{keep} of {len(section)}",
            "sample": section[:keep],
            "more_available": ("Call the specific tool for this area to see the "
                               "rest. Do not describe this sample as complete."),
        }

    return section


async def backup_coverage(stale_after_hours: int = 48) -> dict:
    """Which VMs in vCenter no backup system has ever heard of.

    Veeam can only report on its own roster, so "0 objects without restore
    points" is a statement about the objects already in a job, not about the
    estate. The gap between the two lists is the finding, and it is a join
    across two systems rather than a lookup — done here because a model
    matching 63 names against 11 by hand will quietly get it wrong.
    """
    data = await _gather({
        "vms": ("vcenter_list_vms", {}),
        "veeam": ("veeam_protected", {}),
    })
    vms, veeam = data["vms"], data["veeam"]
    failed = [k for k, v in data.items() if isinstance(v, dict) and v.get("error")]
    if failed:
        return {
            "triage_target": "backup coverage",
            "sections_failed": failed,
            "guidance": ("Coverage could not be determined because "
                         + ", ".join(failed) + " failed. An unavailable check is "
                         "not a passed check — say the estate's backup coverage "
                         "is unknown, not that it is fine."),
            **data,
        }

    vm_rows = vms if isinstance(vms, list) else (vms or {}).get("vms", [])
    objects = (veeam or {}).get("objects", [])

    def key(name):
        # Veeam records a VM under its vCenter display name, but case and the
        # DNS suffix drift between the two inventories.
        text = (name or "").strip().lower()
        return text.split(".")[0] or text

    by_key = {}
    for obj in objects:
        by_key.setdefault(key(obj.get("name")), obj)

    unprotected, stale, protected_rows = [], [], []
    for vm in vm_rows:
        if not isinstance(vm, dict):
            continue
        match = by_key.get(key(vm.get("name")))
        row = {
            "vm": vm.get("name"),
            "power_state": vm.get("power_state"),
            "guest_os": vm.get("guest_os"),
        }
        if match is None:
            row["reason"] = "not present in Veeam at all"
            unprotected.append(row)
        elif not match.get("restore_points"):
            row["reason"] = "known to Veeam but has no restore point"
            unprotected.append(row)
        else:
            age = match.get("newest_restore_point_age_hours")
            row["newest_restore_point"] = match.get("newest_restore_point")
            row["age_hours"] = age
            protected_rows.append(row)
            if age is not None and age > stale_after_hours:
                stale.append(row)

    matched = len(protected_rows)
    return {
        "triage_target": "backup coverage",
        "systems_consulted": ["vCenter", "Veeam"],
        "vms_in_vcenter": len(vm_rows),
        "objects_known_to_veeam": (veeam or {}).get("objects_known_to_veeam"),
        "veeam_roster_complete": (veeam or {}).get("complete"),
        "vms_with_a_restore_point": matched,
        "vms_without_a_restore_point": len(unprotected),
        "vms_with_a_stale_restore_point": len(stale),
        "stale_after_hours": stale_after_hours,
        "unprotected": unprotected,
        "stale": stale,
        "guidance": (
            "vms_without_a_restore_point is the finding, and it outranks every "
            "alarm, licence and hygiene item — it means the data is not "
            "recoverable. Report the count against vms_in_vcenter, list the "
            "affected VMs by name in a table, and distinguish the two reasons: "
            "a VM absent from Veeam was never protected at all, whereas one "
            "present with no restore point means a job is failing. Templates "
            "and powered-off VMs may be excluded deliberately; say which are "
            "which rather than treating every row as an incident. If "
            "veeam_roster_complete is false, Veeam returned only part of its "
            "roster and the gap may be overstated — say so."
        ),
    }


async def triage_estate(full: bool = False) -> dict:
    """Estate-wide health sweep across all five systems.

    Sections are condensed to counts plus a sample unless full is set, because
    the uncondensed sweep is large enough to crowd the context window — at
    which point earlier tool results are silently lost rather than reported
    as missing.
    """
    data = await _gather({
        "vcenter_alarms": ("vcenter_alarms", {}),
        "ops_critical_alerts": ("ops_critical_alerts", {}),
        "network_alerts": ("networks_alerts", {}),
        "datastores_low": ("vcenter_datastores_lowfree", {"threshold_percent": 20}),
        "old_snapshots": ("vcenter_old_snapshots", {}),
        "vmtools_notrunning": ("vcenter_vmtools_notrunning", {}),
        "clusters": ("vcenter_clusters_summary", {}),
        "hosts": ("vcenter_list_hosts", {}),
        "log_errors": ("logs_errors", {"hours": 24, "limit": 50}),
        "failed_backups": ("veeam_failed_jobs", {"hours": 24, "failed_only": True}),
    })
    failed = [k for k, v in data.items() if isinstance(v, dict) and v.get("error")]
    sections = data if full else {k: _condense(v) for k, v in data.items()}
    return {
        "triage_target": "estate",
        "systems_consulted": ["vCenter", "VCF Operations", "VCF Networks", "Logs", "Veeam"],
        "sections_failed": failed,
        "detail_level": "full" if full else "counts plus a sample per section",
        "guidance": (
            "Report by severity, naming specific objects. If sections_failed is "
            "non-empty, say which checks did not run — the estate has not been "
            "fully checked and must not be described as healthy. Where a section "
            "says more_available, the listed items are a sample: either say so or "
            "call that area's own tool for the full set. If failed_backups is "
            "non-empty, treat it as a possible data-loss exposure and rank it "
            "above licensing and hygiene findings; a failed job is not proof a VM "
            "is unprotected, so confirm with restore points before saying so."
        ),
        **sections,
    }


def _version_tuple(text: Any) -> Optional[tuple]:
    """Parse "9.0.2" into (9, 0, 2). None if it is not a plain dotted number."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def _vcenter_host_alignment(vcenter: Any, hosts: Any) -> Optional[dict]:
    """Compare vCenter's version with the ESXi hosts it manages.

    vCenter ahead of its hosts is the supported direction; hosts ahead of
    vCenter is not. Live estate: vCenter 9.0.2, hosts 9.0.1 — a real gap that
    is easy to miss when the two numbers sit in separate sections of an answer.
    Returns None rather than guessing when either side is unparseable.
    """
    if not isinstance(vcenter, dict) or not isinstance(hosts, dict):
        return None
    vc = _version_tuple(vcenter.get("version"))
    host_versions = {b.get("version") for b in hosts.get("builds", [])}
    parsed = {v: _version_tuple(v) for v in host_versions if _version_tuple(v)}
    if not vc or not parsed:
        return None

    behind = sorted(v for v, t in parsed.items() if t < vc)
    ahead = sorted(v for v, t in parsed.items() if t > vc)

    out = {"vcenter_version": vcenter.get("version"),
           "host_versions": sorted(parsed)}
    if ahead:
        out["status"] = "hosts_ahead_of_vcenter"
        out["note"] = (
            f"ESXi {', '.join(ahead)} is newer than vCenter "
            f"{vcenter.get('version')}. vCenter is expected to be at or above "
            "the version of the hosts it manages; this is the wrong way round "
            "and is worth checking."
        )
    elif behind:
        out["status"] = "hosts_behind_vcenter"
        out["note"] = (
            f"Hosts are on {', '.join(behind)} while vCenter is on "
            f"{vcenter.get('version')}. This is the normal direction — vCenter "
            "is upgraded first — but the hosts have not caught up."
        )
    else:
        out["status"] = "aligned"
        out["note"] = "vCenter and all hosts report the same version."
    return out


async def estate_versions() -> dict:
    """What software the estate is running, gathered from every system at once.

    Answering this by hand took four separate tool calls, so it is one call
    here. The uncovered systems are listed explicitly: asked "what are we
    running", a model given only partial data will otherwise present that
    partial list as the whole estate.
    """
    data = await _gather({
        "vcenter": ("vcenter_about", {}),
        "hosts": ("vcenter_list_hosts", {}),
        "logs": ("logs_version", {}),
        "veeam": ("veeam_version", {}),
        "networks": ("networks_version", {}),
        "vm_versions": ("vcenter_vm_versions", {"limit": 1}),
    })

    hosts = data.pop("hosts")
    # /hosts returns a bare list, not {"hosts": [...]}. The first version of
    # this assumed a dict, which meant the grouping silently produced nothing
    # and the alignment comparison was skipped without saying why.
    rows = hosts if isinstance(hosts, list) else None
    if rows is None and isinstance(hosts, dict) and not hosts.get("error"):
        rows = hosts.get("hosts") if isinstance(hosts.get("hosts"), list) else []
    if rows is not None:
        builds = {}
        for row in rows:
            key = (row.get("version"), row.get("build"))
            builds.setdefault(key, []).append(row.get("name"))
        data["esxi_hosts"] = {
            "host_count": len(rows),
            "distinct_builds": len(builds),
            "builds": [
                {"version": v, "build": b, "host_count": len(names),
                 "hosts": sorted(n for n in names if n)}
                for (v, b), names in builds.items()
            ],
        }
    else:
        data["esxi_hosts"] = hosts

    # The grouped summary is what an estate-wide answer needs; the per-VM rows
    # are requested at limit=1 and dropped entirely. vcenter_vm_versions is
    # there for anyone who wants the full list.
    vms = data.get("vm_versions")
    if isinstance(vms, dict) and not vms.get("error"):
        vms.pop("vms", None)
        vms.pop("vms_truncated", None)

    failed = [k for k, v in data.items() if isinstance(v, dict) and v.get("error")]
    alignment = _vcenter_host_alignment(data.get("vcenter"), data.get("esxi_hosts"))
    return {
        "covered": ["vCenter", "ESXi hosts", "VM hardware and VMware Tools",
                    "VCF Operations for Logs",
                    "VCF Operations for Networks", "Veeam Backup & Replication"],
        "not_covered": {
            "NSX": (
                "NSX IS deployed in this estate — nsxmgr01, nsxmgr02 and "
                "nsxmgr03 are visible as VMs in vCenter — but there is no NSX "
                "wrapper, so its version cannot be read. Say the version is "
                "unavailable, never that NSX is absent or not in use."
            ),
            "VCF Operations": "No version endpoint on the VCF Operations wrapper.",
            "SDDC Manager": (
                "sddcmgr01 exists in vCenter but is not integrated, so its "
                "version cannot be read."
            ),
        },
        "sections_failed": failed,
        **({"version_alignment": alignment} if alignment else {}),
        "guidance": (
            "Report the versions found, then state plainly which systems could "
            "not be checked, using not_covered and sections_failed. Do not "
            "present this as the complete estate software inventory, and do not "
            "offer to run a query for an uncovered system — there is no tool "
            "for it. If distinct_builds is greater than 1 the hosts are not on "
            "a uniform build, which is worth calling out. For VM hardware, use "
            "hardware_versions.summary for the on-newest count — do not infer "
            "it from the distribution list, which has been misread as 'most "
            "VMs are on the newest version' when the opposite was true. Treat "
            "being behind the newest as a planning observation, not a fault. "
            "Tools not running on a powered-off VM is normal and should not be "
            "reported as a problem."
        ),
        **data,
    }


# --- Output shaping ----------------------------------------------------------
#
# The chat pane now renders Markdown tables as real tables with a CSV download,
# so tables are wanted rather than repaired away. Earlier this function
# flattened them to "cell - cell" lines because the pane could only display
# text nodes; that was a workaround for a missing renderer, and rows of
# pipe-separated names are unreadable past a handful of items.
#
# HTML is still stripped: the renderer deliberately builds every cell with
# textContent, so markup from the model is neither rendered nor wanted.

_HTML_BREAK = re.compile(r"<br\s*/?>", re.I)

# Opening or closing line of a fenced block, capturing the fence run and the
# info string (```mermaid -> "mermaid").
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")


def _iter_fence_segments(text: str):
    """Split text into (is_fenced, info_string, lines) runs, preserving order.

    Shaping rules that are right for prose are wrong inside a code fence, so
    every transformation below needs to know which side of a fence it is on.
    """
    seg, in_fence, fence, info = [], False, None, ""
    for line in text.split("\n"):
        match = _FENCE.match(line)
        if match and not in_fence:
            if seg:
                yield (False, "", seg)
            in_fence, fence, info = True, match.group(1), (match.group(2) or "").lower()
            seg = [line]
        elif match and in_fence and line.strip().startswith(fence):
            seg.append(line)
            yield (True, info, seg)
            seg, in_fence, fence, info = [], False, None, ""
        else:
            seg.append(line)
    if seg:
        yield (in_fence, info, seg)


def plain_text(answer: str) -> str:
    """Remove HTML that the pane will not render, leaving Markdown tables intact.

    The table rows are passed through untouched for the client-side renderer.

    Fenced blocks are exempt. Stripping ``<br/>`` from inside a code fence
    corrupts the code being displayed rather than cleaning markup, and it also
    silently defeats the one line-break syntax Mermaid labels accept.
    """
    if not answer:
        return answer
    parts = []
    for fenced, _info, lines in _iter_fence_segments(answer):
        block = "\n".join(lines)
        if not fenced:
            block = re.sub(r"\n{3,}", "\n\n", _HTML_BREAK.sub(" ", block))
        parts.append(block)
    return "\n".join(parts)


def _strip_inline_mermaid_comment(line: str) -> str:
    """Drop a trailing ``%%`` comment, leaving ``%%`` inside quoted labels alone.

    Mermaid only accepts a comment on a line of its own. A trailing one such as
    ``win2022 --> sw1001 %% lives on host`` is not ignored, it is a parse error,
    and one of them fails the whole diagram: the reader gets a red box instead
    of a picture. A label like ``x["50%% used"]`` is legitimate, so the scan has
    to track quoting rather than cut at the first ``%%``.
    """
    if line.lstrip().startswith("%%"):
        return line  # whole-line comment, or a %%{init: ...}%% directive
    in_quote = False
    for i in range(len(line) - 1):
        char = line[i]
        if char == '"':
            in_quote = not in_quote
        elif not in_quote and char == "%" and line[i + 1] == "%":
            return line[:i].rstrip()
    return line


def repair_mermaid(answer: str) -> str:
    """Make Mermaid blocks parseable, from code rather than by asking nicely.

    The model was observed emitting trailing ``%%`` comments, which Mermaid
    rejects outright. Prompting reduces it but cannot remove it, and the failure
    is total rather than partial, so the guarantee is enforced here for the same
    reason as _flag_tool_failures.
    """
    if not answer or "mermaid" not in answer.lower():
        return answer
    parts = []
    for fenced, info, lines in _iter_fence_segments(answer):
        if fenced and info == "mermaid" and len(lines) > 1:
            body, closing = lines[1:], []
            if body and _FENCE.match(body[-1]):
                body, closing = body[:-1], [body[-1]]
            lines = [lines[0]] + [_strip_inline_mermaid_comment(b) for b in body] + closing
        parts.append("\n".join(lines))
    return "\n".join(parts)


LOCAL_HANDLERS = {
    "triage_vm": triage_vm,
    "triage_host": triage_host,
    "triage_estate": triage_estate,
    "backup_coverage": backup_coverage,
    "estate_versions": estate_versions,
}


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


def _flag_tool_failures(answer: str, tool_errors: list) -> str:
    """Append a machine-generated notice for any tool that failed.

    The model is asked to report tool failures and usually does, but it has
    been observed answering "122 virtual machines ... No errors were
    encountered" for a call that returned nothing at all. Prompting cannot make
    that reliable, and a component that is usually honest is more dangerous
    than one that is never honest, because nobody learns to distrust it. So the
    warning is emitted from code, where it cannot be talked out of, even at the
    cost of repeating a failure the model already described.
    """
    if not tool_errors:
        return answer
    lines = ["", "---", "**⚠️ Some data could not be retrieved — this answer is incomplete.**", ""]
    seen = set()
    for failure in tool_errors:
        key = (failure["tool"], failure["error"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- `{failure['tool']}` failed: {failure['error']}")
    lines.append("")
    lines.append("Treat any count or list above as unverified.")
    return answer + "\n".join(lines)


async def chat_with_tools(user_message: str, model: str = None, conversation: list = None,
                          scope: str = "all", read_only: bool = False) -> dict:
    """Send a message to Ollama with tool-calling, execute tools, return the answer.

    Returns {"answer", "usage", "tools_called"}.

    Runs an agentic loop: the model may call tools, see the results, and then
    call *more* tools based on what it found. This is what makes cross-system
    questions work ("find the critical alerts, then look up those VMs"), since
    the second lookup depends on the first one's output.

    ``read_only=True`` withholds every state-changing tool regardless of
    ENABLE_WRITE_TOOLS. Scheduled runs use it: nobody is watching a job that
    fires at 07:00, so an unattended run must not be able to propose, let alone
    perform, a change.
    """
    use_model = model or DEFAULT_MODEL
    tools = TOOLS_BY_SCOPE.get(scope, TOOLS)
    if read_only:
        tools = [t for t in tools
                 if not TOOL_SPECS.get(t["function"]["name"], {}).get("write")]
    if conversation is None:
        conversation = [{"role": "system", "content": prompt_for(scope)}]

    conversation.append({"role": "user", "content": user_message})

    usage = Usage()
    tools_called = []
    pending_actions = []
    tool_errors = []

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
                        "answer": _flag_tool_failures(repair_mermaid(content), tool_errors),
                        "usage": usage.as_dict(),
                        "tools_called": tools_called,
                        "pending_actions": pending_actions,
                        "tool_errors": tool_errors,
                    }
                break  # empty answer with no tool calls — force a synthesis pass

            names = [tc["function"]["name"] for tc in tool_calls]
            tools_called.extend(names)

            # Defence in depth. Withholding the schemas should be enough, but a
            # model can invent a name it was never offered, and "the model
            # shouldn't do that" is not a control.
            if read_only:
                refused = [tc for tc in tool_calls
                           if TOOL_SPECS.get(tc["function"]["name"], {}).get("write")]
                if refused:
                    for tc in refused:
                        conversation.append({
                            "role": "tool",
                            "name": tc["function"]["name"],
                            "content": json.dumps({
                                "error": "refused",
                                "reason": "This run is read-only. State-changing "
                                          "tools are not available to unattended "
                                          "or scheduled runs.",
                            }),
                        })
                    tool_calls = [tc for tc in tool_calls if tc not in refused]
                    if not tool_calls:
                        continue

            results = await asyncio.gather(*[
                call_api(tc["function"]["name"], tc["function"].get("arguments", {}))
                for tc in tool_calls
            ])

            # Label each result with its tool name. Without this, parallel calls
            # come back as anonymous blobs the model cannot tell apart.
            for tc, result_data in zip(tool_calls, results):
                # A proposed write must reach the UI, not just the model: the
                # operator is the one who has to confirm it.
                if isinstance(result_data, dict) and result_data.get("confirmation_token"):
                    pending_actions.append({
                        k: result_data[k] for k in
                        ("confirmation_token", "tool", "arguments", "action",
                         "irreversible", "warning")
                        if k in result_data
                    })
                # A failed tool is recorded here, in code, rather than trusted to
                # the model's prose. The model has been observed reporting "no
                # errors were encountered" for a call that returned nothing.
                # Test for the KEY, not its truthiness: a connection timeout
                # surfaces as {"error": ""} because str(exc) is empty for some
                # httpx exceptions, and a falsy check silently drops exactly the
                # failure mode that started all this.
                if isinstance(result_data, dict) and "error" in result_data:
                    detail = str(result_data["error"]).strip()
                    tool_errors.append({
                        "tool": tc["function"]["name"],
                        "error": detail[:300] or "failed without a message (usually a connection timeout)",
                    })
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
            "answer": _flag_tool_failures(repair_mermaid(answer), tool_errors),
            "usage": usage.as_dict(),
            "tools_called": tools_called,
            "pending_actions": pending_actions,
            "tool_errors": tool_errors,
        }


# --- API Endpoints ---

class ChatRequest(BaseModel):
    message: str
    # These must be Optional, not `str = None`. Pydantic skips validating a
    # default, but an explicitly sent JSON null is validated against the
    # annotation and rejected with a 422 — and the UI proxy always sends
    # conversation_id, null included, on a first message.
    model: Optional[str] = None  # Optional model override
    scope: str = "all"  # "all", or one of SYSTEMS: vcenter / vcf_ops / vcf_networks
    conversation_id: Optional[str] = None  # Continue an existing conversation; None starts one

class ChatResponse(BaseModel):
    answer: str
    model: str
    usage: dict = {}
    tools_called: list = []
    telemetry: dict = {}
    pending_actions: list = []
    conversation_id: Optional[str] = None
    history_turns: int = 0  # Prior exchanges replayed into this answer


class ConfirmRequest(BaseModel):
    token: str


@app.get("/pending")
async def list_pending():
    """Write operations proposed but not yet confirmed."""
    _expire_pending()
    return {
        "write_tools_enabled": ENABLE_WRITE_TOOLS,
        "confirmation_required": WRITE_REQUIRE_CONFIRM,
        "pending": [
            {
                "confirmation_token": token,
                "tool": item["tool"],
                "arguments": item["arguments"],
                "irreversible": item["tool"] in IRREVERSIBLE,
                "age_seconds": int(time.time() - item["proposed_at"]),
            }
            for token, item in PENDING.items()
        ],
    }


@app.post("/confirm")
async def confirm_action(request: ConfirmRequest):
    """Execute a proposed write, then re-read the state to prove it happened."""
    if not ENABLE_WRITE_TOOLS:
        raise HTTPException(status_code=403, detail="Write tools are disabled on this server.")
    return await execute_pending(request.token)


@app.post("/cancel")
async def cancel_action(request: ConfirmRequest):
    """Discard a proposed write without executing it."""
    item = PENDING.pop(request.token, None)
    if not item:
        raise HTTPException(status_code=404, detail="Unknown or expired confirmation token.")
    _audit({"event": "cancelled", "token": request.token, "tool": item["tool"],
            "arguments": item["arguments"]})
    return {"status": "CANCELLED", "executed": False, "tool": item["tool"]}


@app.get("/audit")
async def read_audit(limit: int = 50):
    """Recent write activity. The record of what this assistant actually did."""
    try:
        with open(AUDIT_LOG) as handle:
            lines = handle.readlines()[-limit:]
    except FileNotFoundError:
        return {"audit_log": AUDIT_LOG, "entries": [], "note": "No writes recorded yet."}
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"audit_log": AUDIT_LOG, "entries": entries}


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
        # Exposed so the UI describes the systems actually wired up rather
        # than a list written by hand, which went stale the moment logs and
        # backup were added.
        "systems": [{"key": key, "label": spec["label"], "summary": spec["summary"]}
                    for key, spec in SYSTEMS.items()],
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
    ollama, vcenter, ops, networks, logs, backup = await asyncio.gather(
        _probe_ollama(),
        _probe_api("vcenter", VCENTER_BASE, "/health"),
        _probe_api("vcf_ops", OPS_BASE, "/ops/health"),
        _probe_api("vcf_networks", NETWORKS_BASE, "/ni/health"),
        _probe_api("logs", LOGS_BASE, "/logs/health"),
        _probe_api("backup", VEEAM_BASE, "/veeam/health"),
    )

    apis = [vcenter, ops, networks, logs, backup]
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


HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "6"))


def build_conversation(scope: str, conversation_id: str = None) -> tuple:
    """The message list to start from, plus how many prior turns it replays.

    Memory is prose only. Tool results are not replayed: one estate question can
    return 12k tokens of JSON, so three of those would evict the actual question
    from the context window and the model would answer the wrong thing while
    looking perfectly confident.
    """
    messages = [{"role": "system", "content": prompt_for(scope)}]
    if not conversation_id:
        return messages, 0
    prior = store.history(conversation_id, limit_turns=HISTORY_TURNS)
    messages.extend(prior)
    return messages, len(prior) // 2


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
    conversation_id = request.conversation_id or store.create_conversation()
    conversation, replayed = build_conversation(request.scope, request.conversation_id)
    try:
        result = await chat_with_tools(request.message, model=use_model,
                                       conversation=conversation,
                                       scope=request.scope)
        # Persist after the answer, not before: a failed question should not
        # leave a dangling user turn that the next question replays as context.
        store.add_message(conversation_id, "user", request.message)
        store.add_message(conversation_id, "assistant", result["answer"])
        # Telemetry is best-effort decoration; a dead exporter must not fail a
        # question that was answered successfully.
        return ChatResponse(
            answer=plain_text(result["answer"]),
            model=use_model,
            usage=result["usage"],
            tools_called=result["tools_called"],
            pending_actions=result.get("pending_actions", []),
            telemetry=await fetch_telemetry(),
            conversation_id=conversation_id,
            history_turns=replayed,
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


# --- Resident model memory ---------------------------------------------
# The GB10 has one pool of unified memory and no MIG, so a pinned model
# blocks anything else wanting the GPU — an NVIDIA NIM, say. Ollama takes a
# per-request keep_alive that overrides its service default, so the model
# can be released and restored without touching systemd on the inference
# host. A request carrying no prompt loads or evicts without generating.

# A set-but-empty value (compose writes ASSISTANT_MODEL= when unset) must
# fall through to DEFAULT_MODEL — os.getenv's default only covers "absent".
ASSISTANT_MODEL = os.getenv("ASSISTANT_MODEL") or DEFAULT_MODEL


# Ollama reports a far-future sentinel expiry for a pinned model — keep_alive
# of -1 lands in the year 2318 — and a real clock time for one loaded normally.
#
# This distinction only started to matter once the inference host's
# OLLAMA_KEEP_ALIVE default became a 5m idle timeout instead of -1. Under the
# old default every resident model was also pinned, so "is it loaded" and "is
# it held" were the same question. They are not anymore: an ordinary chat
# request now loads the model for a few minutes without pinning it, and
# treating that as pinned would offer an Unpin button for a model that is
# about to release itself anyway.
#
# A day is far outside any plausible idle timeout but far short of the
# sentinel, so it separates the two cases without hardcoding the year.
_PIN_HORIZON_SECONDS = 24 * 60 * 60


def _is_pinned(expires_at: Optional[str]) -> bool:
    """True when a resident model is held indefinitely rather than idling out."""
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        # An unparseable timestamp is not evidence of a pin, and this endpoint
        # is polled every 15s — it must not raise on a surprising value.
        return False
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return (expiry - datetime.now(timezone.utc)).total_seconds() > _PIN_HORIZON_SECONDS


async def _set_keep_alive(model: str, keep_alive) -> None:
    # Pinning reloads the whole model from disk; on a 120B that is tens of
    # seconds, so this timeout is deliberately generous.
    async with httpx.AsyncClient(timeout=900.0) as client:
        response = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": model, "keep_alive": keep_alive},
        )
        response.raise_for_status()


@app.get("/memory")
async def memory_status():
    """Which models are resident on the inference host, and how large."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{OLLAMA_URL}/api/ps")
            response.raise_for_status()
            models = response.json().get("models") or []
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach Ollama at {OLLAMA_URL}: {exc}",
        )

    assistant = next(
        (m for m in models if m.get("name") == ASSISTANT_MODEL), None
    )

    return {
        "assistant_model": ASSISTANT_MODEL,
        # Loaded right now, but possibly only until the idle timeout expires.
        "resident": assistant is not None,
        # Held indefinitely by an explicit pin. See _is_pinned.
        "pinned": _is_pinned(assistant.get("expires_at")) if assistant else False,
        "total_gb": round(sum(m.get("size") or 0 for m in models) / 1024**3, 1),
        "models": [
            {
                "name": m.get("name"),
                "size_gb": round((m.get("size") or 0) / 1024**3, 1),
                "expires_at": m.get("expires_at"),
                "pinned": _is_pinned(m.get("expires_at")),
            }
            for m in models
        ],
    }


@app.post("/memory/pin")
async def memory_pin():
    """Load the assistant model and hold it resident indefinitely."""
    started = time.monotonic()
    try:
        await _set_keep_alive(ASSISTANT_MODEL, -1)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not pin {ASSISTANT_MODEL} — is something else "
                   f"holding the memory? ({exc})",
        )
    return {"pinned": ASSISTANT_MODEL, "seconds": round(time.monotonic() - started, 1)}


@app.post("/memory/unpin")
async def memory_unpin():
    """Evict the assistant model now, freeing its memory for other work."""
    started = time.monotonic()
    try:
        await _set_keep_alive(ASSISTANT_MODEL, 0)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not unpin {ASSISTANT_MODEL}: {exc}",
        )
    return {"unpinned": ASSISTANT_MODEL, "seconds": round(time.monotonic() - started, 1)}


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


# --- Conversations, schedules and stored reports ------------------------------
#
# Memory and scheduling live here rather than in a separate agent because the
# value is in the browser: a follow-up question that knows what "those" refers
# to, and a report that runs at 07:00 whether or not anyone opens the page.

SCHEDULER_TICK = int(os.getenv("SCHEDULER_TICK", "30"))
SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() in ("1", "true", "yes")


class ScheduleRequest(BaseModel):
    question: str
    kind: str = "daily"           # hourly | daily | weekly
    hour: int = 7
    minute: int = 0
    weekday: Optional[int] = None  # 0 = Monday, weekly only
    model: Optional[str] = None
    scope: str = "all"


async def run_scheduled(schedule: dict) -> str:
    """Execute one scheduled question and store the result.

    Read-only, always. Nobody is watching at 07:00, so an unattended run gets
    no state-changing tools even if writes are enabled for interactive use.
    """
    run_id = store.start_run(schedule["question"], schedule_id=schedule["id"],
                             model=schedule.get("model"),
                             scope=schedule.get("scope", "all"))
    try:
        conversation = [{"role": "system",
                         "content": prompt_for(schedule.get("scope", "all"))}]
        # Tell a recurring job what it said last time, so a daily report can
        # lead with what changed instead of restating the estate every morning.
        previous = store.previous_answer(schedule["id"])
        if previous:
            conversation.append({
                "role": "user",
                "content": ("For context, your previous answer to this same "
                            f"scheduled question on {previous['started_at']} was:\n\n"
                            f"{previous['answer']}\n\n"
                            "Lead with what has changed since then. Say so "
                            "explicitly if nothing has."),
            })
            conversation.append({"role": "assistant",
                                 "content": "Understood. I will report changes since then."})
        result = await chat_with_tools(
            schedule["question"], model=schedule.get("model"),
            conversation=conversation, scope=schedule.get("scope", "all"),
            read_only=True)
        store.finish_run(run_id, answer=plain_text(result["answer"]),
                         tools_called=result["tools_called"],
                         usage=result["usage"])
    except Exception as exc:
        # A failed run is recorded, not swallowed. A schedule that silently
        # stopped producing reports is the failure mode worth avoiding.
        store.finish_run(run_id, error=f"{type(exc).__name__}: {exc}")
    return run_id


async def scheduler_loop() -> None:
    """Fire due schedules, one at a time.

    Sequential on purpose: these are 30-second-to-5-minute tool-calling runs
    against five production APIs, and three firing at once would be a
    self-inflicted load test.
    """
    while True:
        try:
            now = datetime.now(timezone.utc)
            for schedule in store.due_schedules(now.isoformat(timespec="seconds")):
                # Move the schedule forward *before* running it. A run that
                # crashes the process must not leave the job permanently due and
                # re-firing on every restart.
                following = schedule_times.catch_up(
                    schedule["kind"], schedule["hour"], schedule["minute"],
                    schedule["weekday"], now=now)
                store.mark_schedule_ran(
                    schedule["id"], following.isoformat(timespec="seconds"))
                await run_scheduled(schedule)
        except Exception as exc:  # keep ticking; a bad row must not stop the loop
            print(f"[scheduler] tick failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(SCHEDULER_TICK)


@app.get("/conversations")
async def get_conversations(limit: int = 50):
    return {"conversations": store.list_conversations(limit=limit)}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    messages = store.history(conversation_id, limit_turns=1000)
    if not messages:
        raise HTTPException(status_code=404, detail="No such conversation")
    return {"conversation_id": conversation_id, "messages": messages}


@app.delete("/conversations/{conversation_id}")
async def remove_conversation(conversation_id: str):
    store.delete_conversation(conversation_id)
    return {"deleted": conversation_id}


@app.get("/schedules")
async def get_schedules():
    items = store.list_schedules()
    for item in items:
        item["description"] = schedule_times.describe(
            item["kind"], item["hour"], item["minute"], item["weekday"])
    return {"schedules": items, "scheduler_running": SCHEDULER_ENABLED,
            "now": store.utcnow()}


@app.post("/schedules")
async def add_schedule(request: ScheduleRequest):
    if request.scope not in TOOLS_BY_SCOPE:
        raise HTTPException(status_code=400,
                            detail=f"Unknown scope: {request.scope}")
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="A schedule needs a question")
    try:
        first = schedule_times.next_due(request.kind, request.hour,
                                        request.minute, request.weekday)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sid = store.create_schedule(
        request.question.strip(), request.kind, request.hour, request.minute,
        weekday=request.weekday, model=request.model, scope=request.scope,
        next_run=first.isoformat(timespec="seconds"))
    return {"id": sid, "next_run": first.isoformat(timespec="seconds"),
            "description": schedule_times.describe(
                request.kind, request.hour, request.minute, request.weekday)}


@app.delete("/schedules/{schedule_id}")
async def remove_schedule(schedule_id: str):
    if not store.get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="No such schedule")
    store.delete_schedule(schedule_id)
    return {"deleted": schedule_id}


@app.post("/schedules/{schedule_id}/enabled")
async def toggle_schedule(schedule_id: str, enabled: bool = True):
    if not store.get_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="No such schedule")
    store.set_schedule_enabled(schedule_id, enabled)
    return {"id": schedule_id, "enabled": enabled}


@app.post("/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str):
    """Run a schedule immediately, without waiting for its slot.

    The point is to see the report a schedule will produce before trusting it to
    run unattended at 07:00.
    """
    schedule = store.get_schedule(schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="No such schedule")
    run_id = await run_scheduled(schedule)
    return store.get_run(run_id)


@app.get("/runs")
async def get_runs(limit: int = 50, schedule_id: Optional[str] = None):
    return {"runs": store.list_runs(limit=limit, schedule_id=schedule_id)}


@app.get("/runs/{run_id}")
async def get_single_run(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="No such run")
    return run


# --- OpenAI-compatible surface ------------------------------------------------
#
# Lets any OpenAI-speaking client (Open WebUI, in practice) drive the assistant,
# so the tools are reachable from a general chat UI and not only the bespoke one.
# The bespoke UI keeps the things a plain chat client cannot express: schedules,
# reports, CSV export, pin/unpin, the telemetry bar.
#
# SCOPE IS THE MODEL ID. A chat client has exactly one selector, and scope --
# which of the tools the model may call -- is the choice worth spending it on.
# The underlying LLM stays DEFAULT_MODEL.
#
# STATE LIVES IN THE CLIENT. /chat keeps conversations server-side by
# conversation_id; an OpenAI client instead resends the whole thread each turn.
# So this path is deliberately stateless: it rebuilds context from the supplied
# messages and writes nothing to the conversation store, which keeps the two
# front ends from interleaving turns into each other's histories.

OPENAI_MODEL_PREFIX = "assistant-"

# "confirm <token>" / "cancel <token>", typed as an ordinary message. A chat
# client has no buttons, so the two-step approval for writes has to be
# expressible as text or it cannot be completed at all -- and silently
# auto-approving writes because the UI is inconvenient is not an option.
_APPROVAL_RE = re.compile(r"^\s*(confirm|cancel)\s+([A-Za-z0-9._:\-]+)\s*$", re.IGNORECASE)


def _scope_from_model_id(model_id: Optional[str]) -> str:
    """Map an OpenAI model id back to a tool scope, tolerantly."""
    name = (model_id or "").split("/")[-1]
    if name.startswith(OPENAI_MODEL_PREFIX):
        name = name[len(OPENAI_MODEL_PREFIX):]
    # An unknown id must not silently widen the tool set to everything.
    return name if name in TOOLS_BY_SCOPE else "all"


def _flatten_content(content) -> str:
    """OpenAI content is a string, or a list of typed parts when images ride along."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _has_image(messages: list) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def _describe_pending(pending_actions: list) -> str:
    """Spell out proposed writes and how to approve them, since there is no button."""
    if not pending_actions:
        return ""
    lines = ["", "---", "**Proposed changes — not yet applied.**", ""]
    for action in pending_actions:
        token = action.get("confirmation_token", "?")
        mark = " ⚠️ irreversible" if action.get("irreversible") else ""
        lines.append(f"- `{action.get('tool')}` {action.get('arguments')}{mark}")
        if action.get("warning"):
            lines.append(f"  - {action['warning']}")
        lines.append(f"  - approve with `confirm {token}`, discard with `cancel {token}`")
    return "\n".join(lines)


def _openai_response(model_id: str, text: str, usage: dict) -> dict:
    return {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": (usage or {}).get("prompt_tokens", 0),
            "completion_tokens": (usage or {}).get("completion_tokens", 0),
            "total_tokens": (usage or {}).get("total_tokens", 0),
        },
    }


def _sse_stream(payload: dict):
    """One-shot SSE.

    The tool loop runs many rounds before there is anything to say, so there is
    no partial text to stream. Clients still default to stream=true, and a
    client waiting for an SSE frame it never gets just hangs, so answer in the
    shape it asked for.
    """
    created = payload["created"]
    base = {"id": payload["id"], "object": "chat.completion.chunk",
            "created": created, "model": payload["model"]}
    first = dict(base, choices=[{"index": 0, "delta": {"role": "assistant"},
                                 "finish_reason": None}])
    body = dict(base, choices=[{
        "index": 0,
        "delta": {"content": payload["choices"][0]["message"]["content"]},
        "finish_reason": None}])
    last = dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
    for chunk in (first, body, last):
        yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/v1/models")
async def openai_models():
    """One entry per scope, so the client's model picker chooses the tool set."""
    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": f"{OPENAI_MODEL_PREFIX}{scope}",
                "object": "model",
                "created": created,
                "owned_by": "operational-intelligence",
            }
            for scope in sorted(TOOLS_BY_SCOPE)
        ],
    }


@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """Answer an OpenAI-shaped request using the same tool loop as /chat."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Body must be JSON.")

    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty list.")

    model_id = body.get("model") or f"{OPENAI_MODEL_PREFIX}all"
    scope = _scope_from_model_id(model_id)
    stream = bool(body.get("stream"))

    # Drop the client's own system prompt: prompt_for(scope) is what teaches the
    # model these tools exist, and a generic "you are a helpful assistant" on top
    # of it produces confident answers that never call a tool.
    turns = [m for m in messages if m.get("role") in ("user", "assistant")]
    if not turns or turns[-1].get("role") != "user":
        raise HTTPException(status_code=400, detail="The last message must be from the user.")

    latest = _flatten_content(turns[-1].get("content")).strip()
    if not latest:
        if _has_image(messages):
            raise HTTPException(
                status_code=400,
                detail="This assistant is text-only: it answers from the vCenter, "
                       "VCF Operations, logs and Veeam APIs rather than from images.",
            )
        raise HTTPException(status_code=400, detail="The last user message is empty.")

    # Approval typed as text, handled before the model sees it.
    approval = _APPROVAL_RE.match(latest)
    if approval:
        verb, token = approval.group(1).lower(), approval.group(2)
        try:
            if verb == "confirm":
                result = await confirm_action(ConfirmRequest(token=token))
                text = f"Executed `{result.get('tool')}`.\n\n```json\n{json.dumps(result, indent=2, default=str)}\n```"
            else:
                result = await cancel_action(ConfirmRequest(token=token))
                text = f"Cancelled `{result.get('tool')}`. Nothing was changed."
        except HTTPException as exc:
            text = f"Could not {verb} `{token}`: {exc.detail}"
        payload = _openai_response(model_id, text, {})
        if stream:
            return StreamingResponse(_sse_stream(payload), media_type="text/event-stream")
        return payload

    # Prose history only, mirroring build_conversation: replaying tool results
    # would spend the context window on JSON instead of the question.
    history = [
        {"role": m["role"], "content": _flatten_content(m.get("content"))}
        for m in turns[:-1]
    ]
    history = [m for m in history if m["content"].strip()][-(HISTORY_TURNS * 2):]
    conversation = [{"role": "system", "content": prompt_for(scope)}] + history

    try:
        result = await chat_with_tools(
            latest,
            model=DEFAULT_MODEL,
            conversation=conversation,
            scope=scope,
        )
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach Ollama at {OLLAMA_URL} — is it running and reachable?",
        )

    text = plain_text(result["answer"]) + _describe_pending(result.get("pending_actions", []))
    payload = _openai_response(model_id, text, result.get("usage") or {})
    if stream:
        return StreamingResponse(_sse_stream(payload), media_type="text/event-stream")
    return payload


if __name__ == "__main__":
    import uvicorn
    # Loopback by default. This API has no authentication of its own and every
    # tool on it reaches a production system, so the UI in front of it enforcing
    # identity is worth nothing if this port is open on the LAN beside it.
    bind = os.getenv("ORCHESTRATOR_BIND", "127.0.0.1")
    # Log the RESOLVED config, not just the port. A misconfigured MCP_SERVER or
    # OLLAMA_URL produces confident wrong answers rather than a crash, and with
    # nothing in the journal there is no way to tell after the fact which values
    # a running process actually had. That gap cost a production outage.
    print(f"[orchestrator] bind={bind}:8090 tools={len(TOOLS)}", flush=True)
    print(f"[orchestrator] mcp_server={MCP_SERVER}", flush=True)
    print(f"[orchestrator] ollama_url={OLLAMA_URL} default_model={DEFAULT_MODEL}", flush=True)
    print(f"[orchestrator] write_tools={ENABLE_WRITE_TOOLS} require_confirm={WRITE_REQUIRE_CONFIRM}", flush=True)
    uvicorn.run(app, host=bind, port=8090)
