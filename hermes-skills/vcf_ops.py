"""
Hermes Agent skill: VCF Operations API queries
Place this file in ~/.hermes/skills/vcf_ops.py on the Photon VM
"""
import urllib.request
import json

MCP_SERVER = "http://10.0.0.140:8081"

ENDPOINTS = {
    "summary": "/ops/summary",
    "alerts": "/ops/alerts",
    "critical_alerts": "/ops/critical-alerts",
    "top_alerts": "/ops/top-alerts",
    "resources_search": "/ops/resources/search",
    "recommendations": "/ops/recommendations",
    "symptoms": "/ops/symptoms",
}


def run(input: str, **kwargs) -> str:
    """Query VCF Operations Manager for health, alerts, and recommendations.

    Available commands:
    - summary: Overall environment health summary
    - alerts: List all active alerts
    - critical_alerts: Show only critical/immediate alerts
    - top_alerts: Most frequent alert types
    - resources_search [query]: Search monitored resources by name
    - recommendations: Active optimization recommendations
    - symptoms: Current symptoms detected

    Example: "critical_alerts" or "resources_search esxi"
    """
    parts = input.strip().split(maxsplit=1)
    command = parts[0] if parts else "summary"
    query_param = parts[1] if len(parts) > 1 else None

    if command not in ENDPOINTS:
        return f"Unknown command: {command}. Available: {', '.join(ENDPOINTS.keys())}"

    url = f"{MCP_SERVER}{ENDPOINTS[command]}"
    if query_param and "search" in command:
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
        return f"Error querying VCF Operations API: {e}"
