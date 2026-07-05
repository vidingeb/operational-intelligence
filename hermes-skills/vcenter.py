"""
Hermes Agent skill: vCenter API queries
Place this file in ~/.hermes/skills/vcenter.py on the Photon VM
"""
import urllib.request
import json

MCP_SERVER = "http://10.0.0.140:8080"

ENDPOINTS = {
    "list_hosts": "/hosts",
    "list_vms": "/vms",
    "search_vms": "/vms/search",
    "vm_details": "/vm/details",
    "host_usage": "/hosts/usage",
    "datastores": "/datastores",
    "alarms": "/alarms",
    "clusters": "/clusters/summary",
    "recent_tasks": "/tasks/recent",
    "powered_off_vms": "/vms/poweredoff",
    "old_snapshots": "/snapshots/old",
}


def run(input: str, **kwargs) -> str:
    """Query VMware vCenter for infrastructure data.

    Available commands:
    - list_hosts: Show all ESXi hosts
    - list_vms: Show all virtual machines
    - search_vms [query]: Search VMs by name
    - vm_details [vm_id]: Get details for a specific VM
    - host_usage: Show CPU/memory usage per host
    - datastores: List all datastores with capacity
    - alarms: Show active alarms
    - clusters: Show cluster summary
    - recent_tasks: Show recent vCenter tasks
    - powered_off_vms: List powered-off VMs
    - old_snapshots: Find snapshots older than 7 days

    Example: "list_vms" or "search_vms vcf"
    """
    parts = input.strip().split(maxsplit=1)
    command = parts[0] if parts else "list_vms"
    query_param = parts[1] if len(parts) > 1 else None

    if command not in ENDPOINTS:
        return f"Unknown command: {command}. Available: {', '.join(ENDPOINTS.keys())}"

    url = f"{MCP_SERVER}{ENDPOINTS[command]}"
    if query_param:
        url += f"?query={urllib.parse.quote(query_param)}" if "search" in command else f"?vm_id={query_param}"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            result = json.dumps(data, indent=2)
            # Truncate if too long
            if len(result) > 4000:
                result = result[:4000] + "\n... (truncated)"
            return result
    except Exception as e:
        return f"Error querying vCenter API: {e}"
