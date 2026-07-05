"""
Hermes Agent skill: VCF Networks API queries
Place this file in ~/.hermes/skills/vcf_networks.py on the Photon VM
"""
import urllib.request
import json

MCP_SERVER = "http://10.0.0.140:8082"

ENDPOINTS = {
    "search": "/ni/search",
    "alerts": "/ni/alerts",
    "vms": "/ni/vms",
    "nsx_segments": "/ni/entities/nsx-segments",
    "hosts": "/ni/hosts",
    "clusters": "/ni/clusters",
}


def run(input: str, **kwargs) -> str:
    """Query VCF Networks (Network Insight) for topology and traffic analysis.

    Available commands:
    - search [query]: Search network entities (VMs, segments, flows)
    - alerts: Network-related alerts
    - vms: VMs from network perspective (IPs, segments, flows)
    - nsx_segments: List NSX segments/overlays
    - hosts: Hosts from network perspective
    - clusters: Clusters from network perspective

    Example: "alerts" or "search web-server"
    """
    parts = input.strip().split(maxsplit=1)
    command = parts[0] if parts else "alerts"
    query_param = parts[1] if len(parts) > 1 else None

    if command not in ENDPOINTS:
        return f"Unknown command: {command}. Available: {', '.join(ENDPOINTS.keys())}"

    url = f"{MCP_SERVER}{ENDPOINTS[command]}"
    if query_param and command == "search":
        url += f"?query={urllib.parse.quote(query_param)}"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            result = json.dumps(data, indent=2)
            if len(result) > 4000:
                result = result[:4000] + "\n... (truncated)"
            return result
    except Exception as e:
        return f"Error querying VCF Networks API: {e}"
