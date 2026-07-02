from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
import urllib3
from typing import Any, Dict, Optional, List

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(
    title="VCF Operations Local API",
    version="2.0.0",
    description="Local API wrapper for VCF Operations / Aria Operations 9.0 public API"
)

# Optional, useful when testing from browser/tools on the MAP server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
OPS_URL = os.getenv("OPS_URL", "https://vcf-ops01.vcf.local").rstrip("/")
OPS_USER = os.getenv("OPS_USER", "admin")
OPS_PASS = os.getenv("OPS_PASSWORD")
OPS_AUTH_SOURCE = os.getenv("OPS_AUTH_SOURCE", "local")
VERIFY_SSL = os.getenv("OPS_VERIFY_SSL", "false").lower() == "true"
TIMEOUT = int(os.getenv("OPS_TIMEOUT", "60"))


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _require_password() -> None:
    if not OPS_PASS:
        raise HTTPException(
            status_code=500,
            detail="OPS_PASSWORD environment variable is not set on the MAP server."
        )


def get_token() -> str:
    """Acquire a VCF Operations token using local/AD auth."""
    _require_password()
    url = f"{OPS_URL}/suite-api/api/auth/token/acquire"
    payload = {
        "username": OPS_USER,
        "password": OPS_PASS,
        "authSource": OPS_AUTH_SOURCE,
    }

    try:
        r = requests.post(
            url,
            json=payload,
            verify=VERIFY_SSL,
            timeout=TIMEOUT,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()["token"]
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=r.status_code,
            detail=f"VCF Operations token acquire failed: {str(e)} - {r.text[:500]}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"VCF Operations token acquire failed: {str(e)}")


def _headers() -> Dict[str, str]:
    token = get_token()
    return {
        "Authorization": f"vRealizeOpsToken {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def ops_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET helper for public VCF Operations APIs."""
    url = f"{OPS_URL}{path}"
    try:
        r = requests.get(url, params=params, verify=VERIFY_SSL, timeout=TIMEOUT, headers=_headers())
        r.raise_for_status()
        if not r.text:
            return {}
        return r.json()
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=r.status_code,
            detail=f"VCF Operations GET failed for {path}: {str(e)} - {r.text[:500]}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"VCF Operations GET failed for {path}: {str(e)}")


def ops_post(path: str, body: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
    """POST helper for public VCF Operations APIs."""
    url = f"{OPS_URL}{path}"
    try:
        r = requests.post(
            url,
            json=body or {},
            params=params,
            verify=VERIFY_SSL,
            timeout=TIMEOUT,
            headers=_headers(),
        )
        r.raise_for_status()
        if not r.text:
            return {}
        return r.json()
    except requests.HTTPError as e:
        raise HTTPException(
            status_code=r.status_code,
            detail=f"VCF Operations POST failed for {path}: {str(e)} - {r.text[:500]}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"VCF Operations POST failed for {path}: {str(e)}")


def _resource_list(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return data.get("resourceList") or data.get("resources") or []


def _alert_list(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return data.get("alerts") or data.get("alert") or []


# -----------------------------------------------------------------------------
# Basic / existing endpoints
# -----------------------------------------------------------------------------
@app.get("/ops/health")
def health():
    return {
        "status": "ok",
        "ops_url": OPS_URL,
        "ops_user": OPS_USER,
        "auth_source": OPS_AUTH_SOURCE,
        "verify_ssl": VERIFY_SSL,
        "api_version": "2.0.0",
        "note": "Password is read from OPS_PASSWORD environment variable and is not returned here.",
    }


@app.get("/ops/auth/test")
def auth_test():
    token = get_token()
    return {"status": "ok", "token_acquired": True, "token_preview": token[:8] + "..."}


@app.get("/ops/resources")
def resources(page: int = 0, pageSize: int = 1000):
    return ops_get("/suite-api/api/resources", params={"page": page, "pageSize": pageSize})


@app.get("/ops/resources/search")
def resources_search(name: str = Query(..., description="Resource name or partial name"), pageSize: int = 1000):
    data = ops_get("/suite-api/api/resources", params={"pageSize": pageSize})
    items = _resource_list(data)
    matches = [r for r in items if name.lower() in str(r.get("resourceKey", {}).get("name", r.get("name", ""))).lower()]
    return {"query": name, "count": len(matches), "resources": matches}


@app.get("/ops/resource/{resource_id}")
def resource_by_id(resource_id: str):
    return ops_get(f"/suite-api/api/resources/{resource_id}")


@app.get("/ops/alerts")
def alerts(activeOnly: bool = True, page: int = 0, pageSize: int = 1000):
    return ops_get(
        "/suite-api/api/alerts",
        params={"activeOnly": str(activeOnly).lower(), "page": page, "pageSize": pageSize},
    )


@app.get("/ops/critical-alerts")
def critical_alerts():
    data = ops_get("/suite-api/api/alerts", params={"activeOnly": "true", "pageSize": 1000})
    alerts_list = _alert_list(data)
    critical = [
        a for a in alerts_list
        if str(a.get("criticality", "")).lower() in ["critical", "immediate"]
        or str(a.get("severity", "")).lower() in ["critical", "immediate"]
    ]
    return {"critical_alert_count": len(critical), "alerts": critical}


@app.get("/ops/top-alerts")
def top_alerts(limit: int = 10):
    data = ops_get("/suite-api/api/alerts", params={"activeOnly": "true", "pageSize": 1000})
    alerts_list = _alert_list(data)
    priority = {"immediate": 0, "critical": 1, "warning": 2, "info": 3}
    sorted_alerts = sorted(
        alerts_list,
        key=lambda a: priority.get(str(a.get("criticality", a.get("severity", "info"))).lower(), 99),
    )
    return {"count": min(limit, len(sorted_alerts)), "alerts": sorted_alerts[:limit]}


@app.get("/ops/summary")
def summary():
    resources_data = ops_get("/suite-api/api/resources", params={"pageSize": 1000})
    alerts_data = ops_get("/suite-api/api/alerts", params={"activeOnly": "true", "pageSize": 1000})

    resources_list = _resource_list(resources_data)
    alerts_list = _alert_list(alerts_data)
    critical = [
        a for a in alerts_list
        if str(a.get("criticality", "")).lower() in ["critical", "immediate"]
        or str(a.get("severity", "")).lower() in ["critical", "immediate"]
    ]

    return {
        "resource_count": len(resources_list),
        "active_alert_count": len(alerts_list),
        "critical_alert_count": len(critical),
    }


# -----------------------------------------------------------------------------
# Resource details: properties, stat keys and stats
# -----------------------------------------------------------------------------
@app.get("/ops/resource/{resource_id}/properties")
def resource_properties(resource_id: str):
    return ops_get(f"/suite-api/api/resources/{resource_id}/properties")


@app.get("/ops/resource/{resource_id}/statkeys")
def resource_statkeys(resource_id: str):
    return ops_get(f"/suite-api/api/resources/{resource_id}/statkeys")


@app.get("/ops/resource/{resource_id}/stats/latest")
def resource_stats_latest(resource_id: str, statKey: Optional[str] = None):
    params: Dict[str, Any] = {}
    if statKey:
        params["statKey"] = statKey
    return ops_get(f"/suite-api/api/resources/{resource_id}/stats/latest", params=params)


@app.post("/ops/resources/stats/latest")
def resources_stats_latest(body: Dict[str, Any] = Body(...)):
    """
    Proxy for latest stats.
    Expected VCF Ops body normally includes resourceId/resourceIds and statKey/statKeys.
    """
    return ops_post("/suite-api/api/resources/stats/latest", body=body)


# -----------------------------------------------------------------------------
# Cost / chargeback - public API only
# -----------------------------------------------------------------------------
@app.get("/ops/cost-drivers")
def cost_drivers_capability():
    return {
        "status": "informational_only",
        "message": "VCF Operations 9.0 public API exposes cost configuration currency and chargeback APIs. Direct cost-driver endpoints are internal and should not be used from Copilot.",
        "public_cost_endpoints_in_this_wrapper": [
            "/ops/cost/currency",
            "/ops/chargeback/bills/summary",
            "/ops/chargeback/reports",
        ],
        "cost_driver_types_in_ui": [
            "server_hardware",
            "storage",
            "license",
            "applications",
            "maintenance",
            "labor",
            "network",
            "facilities",
            "additional",
        ],
    }


@app.get("/ops/cost-drivers/summary")
def cost_drivers_summary():
    return {
        "status": "available_as_guidance",
        "message": "Direct cost-driver readout is not exposed as a supported public VCF Operations 9.0 API in the Broadcom API reference. Use chargeback reports/bills and cost currency instead.",
        "recommended_endpoints": [
            "/ops/cost/currency",
            "/ops/chargeback/bills/summary",
            "/ops/chargeback/reports",
        ],
    }


@app.get("/ops/cost/currency")
def cost_currency():
    return ops_get("/suite-api/api/costconfig/currency")


@app.post("/ops/chargeback/bills/summary")
def chargeback_bills_summary(
    body: Dict[str, Any] = Body(default_factory=dict),
    page: int = 0,
    pageSize: int = 1000,
    sortBy: Optional[str] = None,
    sortOrder: str = "DESC",
):
    params: Dict[str, Any] = {"page": page, "pageSize": pageSize, "sortOrder": sortOrder}
    if sortBy:
        params["sortBy"] = sortBy
    return ops_post("/suite-api/api/chargeback/bills/query", body=body, params=params)


@app.get("/ops/chargeback/reports")
def chargeback_reports(
    name: Optional[str] = None,
    subject: Optional[str] = None,
    status: Optional[str] = None,
    resourceId: Optional[str] = None,
    page: int = 0,
    pageSize: int = 1000,
):
    params: Dict[str, Any] = {"page": page, "pageSize": pageSize}
    if name:
        params["name"] = name
    if subject:
        params["subject"] = subject
    if status:
        params["status"] = status
    if resourceId:
        params["resourceId"] = resourceId
    return ops_get("/suite-api/api/chargeback/reports", params=params)


@app.get("/ops/chargeback/reports/{report_id}")
def chargeback_report(report_id: str):
    return ops_get(f"/suite-api/api/chargeback/reports/{report_id}")


# -----------------------------------------------------------------------------
# Reports and report definitions
# -----------------------------------------------------------------------------
@app.get("/ops/reports")
def reports(page: int = 0, pageSize: int = 1000):
    return ops_get("/suite-api/api/reports", params={"page": page, "pageSize": pageSize})


@app.get("/ops/report-definitions")
def report_definitions(page: int = 0, pageSize: int = 1000):
    return ops_get("/suite-api/api/reportdefinitions", params={"page": page, "pageSize": pageSize})


# -----------------------------------------------------------------------------
# Policies, super metrics, symptoms, recommendations
# -----------------------------------------------------------------------------
@app.get("/ops/policies")
def policies(page: int = 0, pageSize: int = 1000):
    return ops_get("/suite-api/api/policies", params={"page": page, "pageSize": pageSize})


@app.get("/ops/supermetrics")
def supermetrics(page: int = 0, pageSize: int = 1000):
    return ops_get("/suite-api/api/supermetrics", params={"page": page, "pageSize": pageSize})


@app.get("/ops/symptoms")
def symptoms(page: int = 0, pageSize: int = 1000):
    return ops_get("/suite-api/api/symptoms", params={"page": page, "pageSize": pageSize})


@app.get("/ops/recommendations")
def recommendations(page: int = 0, pageSize: int = 1000):
    return ops_get("/suite-api/api/recommendations", params={"page": page, "pageSize": pageSize})


# -----------------------------------------------------------------------------
# Optimization / Workload Placement - read-only/status endpoints
# -----------------------------------------------------------------------------
@app.get("/ops/optimization/{datacenter_id}/automation/status")
def optimization_automation_status(datacenter_id: str):
    return ops_get(f"/suite-api/api/optimization/workloadplacement/{datacenter_id}/automation/status")


@app.get("/ops/optimization/{datacenter_id}/cross-dc-move/status")
def optimization_cross_dc_move_status(datacenter_id: str):
    return ops_get(f"/suite-api/api/optimization/workloadplacement/{datacenter_id}/crossdcmove/status")


@app.get("/ops/optimization/{datacenter_id}/placement/settings")
def optimization_placement_settings(datacenter_id: str):
    return ops_get(f"/suite-api/api/optimization/workloadplacement/{datacenter_id}/placement/settings")


# -----------------------------------------------------------------------------
# Connector helper: one endpoint that lists what Copilot can use
# -----------------------------------------------------------------------------
@app.get("/ops/commands")
def commands():
    return {
        "read_only_commands": [
            "GET /ops/health",
            "GET /ops/auth/test",
            "GET /ops/summary",
            "GET /ops/resources",
            "GET /ops/resources/search?name=<name>",
            "GET /ops/resource/{resource_id}",
            "GET /ops/resource/{resource_id}/properties",
            "GET /ops/resource/{resource_id}/statkeys",
            "GET /ops/resource/{resource_id}/stats/latest",
            "POST /ops/resources/stats/latest",
            "GET /ops/alerts",
            "GET /ops/critical-alerts",
            "GET /ops/top-alerts",
            "GET /ops/cost-drivers",
            "GET /ops/cost-drivers/summary",
            "GET /ops/cost/currency",
            "POST /ops/chargeback/bills/summary",
            "GET /ops/chargeback/reports",
            "GET /ops/chargeback/reports/{report_id}",
            "GET /ops/reports",
            "GET /ops/report-definitions",
            "GET /ops/policies",
            "GET /ops/supermetrics",
            "GET /ops/symptoms",
            "GET /ops/recommendations",
            "GET /ops/optimization/{datacenter_id}/automation/status",
            "GET /ops/optimization/{datacenter_id}/cross-dc-move/status",
            "GET /ops/optimization/{datacenter_id}/placement/settings",
        ],
        "not_included_by_design": [
            "Internal cost-driver APIs under /suite-api/internal/costdrivers/*",
            "PUT/POST/DELETE operations that change optimization, bills, reports, policies or currency",
        ],
    }