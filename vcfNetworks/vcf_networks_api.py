import os
import re
import time
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

VERIFY_SSL = os.getenv("NI_VERIFY_SSL", "false").lower() == "true"

NI_BASE_URL = os.getenv("NI_BASE_URL", "https://vcfnetworks.vcf.local").rstrip("/")
NI_USERNAME = os.getenv("NI_USERNAME", "")
NI_PASSWORD = os.getenv("NI_PASSWORD", "")

# API-token auth uses domain_type LOCAL/LDAP.
# Your direct test confirmed LOCAL works for /api/ni/auth/token.
NI_DOMAIN_TYPE = os.getenv("NI_DOMAIN_TYPE", os.getenv("NI_DOMAIN", "LOCAL"))

REQUEST_TIMEOUT = int(os.getenv("NI_TIMEOUT", "30"))

app = FastAPI(
    title="Local VCF Operations for Networks API Proxy",
    version="1.5.0",
    description="Local proxy API for Copilot Studio / agent access to VCF Operations for Networks."
)


class LoginRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    domain_type: Optional[str] = None


class SearchRequest(BaseModel):
    entity_type: str = "VirtualMachine"
    filter: str = "name like 'vm'"
    size: int = 50


class PathRequest(BaseModel):
    source: str
    destination: str
    port: Optional[str] = None
    protocol: Optional[str] = None


class NIClient:
    def __init__(self):
        self.base_url = NI_BASE_URL
        self.token: Optional[str] = None
        self.token_expiry: Optional[int] = None
        self.login_time: Optional[float] = None

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        if self.token:
            headers["Authorization"] = f"NetworkInsight {self.token}"

        return headers

    def login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        domain_type: Optional[str] = None
    ) -> Dict[str, Any]:
        user = username or NI_USERNAME
        pwd = password or NI_PASSWORD
        auth_domain_type = domain_type or NI_DOMAIN_TYPE

        if not user or not pwd:
            raise HTTPException(
                status_code=500,
                detail="NI_USERNAME and NI_PASSWORD must be configured, or supplied to /ni/login."
            )

        if not auth_domain_type:
            raise HTTPException(
                status_code=500,
                detail="NI_DOMAIN_TYPE must be configured, for example LOCAL."
            )

        url = f"{self.base_url}/api/ni/auth/token"

        payload = {
            "username": user,
            "password": pwd,
            "domain": {
                "domain_type": auth_domain_type
            }
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                verify=VERIFY_SSL,
                timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to connect to VCF Operations for Networks: {exc}"
            )

        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        try:
            data = response.json()
        except ValueError:
            raise HTTPException(status_code=500, detail=f"Login returned non-JSON response: {response.text}")

        token = data.get("token")
        if not token:
            raise HTTPException(status_code=500, detail=f"Login succeeded but no token found in response: {data}")

        self.token = token
        self.token_expiry = data.get("expiry")
        self.login_time = time.time()

        return {
            "status": "ok",
            "message": "Authenticated against VCF Operations for Networks API using token auth.",
            "base_url": self.base_url,
            "domain_type": auth_domain_type,
            "token_present": True,
            "expiry": self.token_expiry
        }

    def request(self, method: str, path: str, **kwargs) -> Any:
        if not self.token:
            self.login()

        url = f"{self.base_url}{path}"

        try:
            response = requests.request(
                method,
                url,
                headers=self._headers(),
                verify=VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
                **kwargs
            )
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"API request failed: {exc}")

        if response.status_code in [401, 403]:
            self.login()
            response = requests.request(
                method,
                url,
                headers=self._headers(),
                verify=VERIFY_SSL,
                timeout=REQUEST_TIMEOUT,
                **kwargs
            )

        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)

        if not response.text:
            return {}

        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}


client = NIClient()


def build_search_filter(query: str) -> str:
    q = query.strip()

    filter_keywords = [
        " like ",
        " = ",
        "!=",
        " contains ",
        " in ",
        " and ",
        " or ",
        ">",
        "<"
    ]

    if any(keyword in q.lower() for keyword in filter_keywords):
        return q

    safe_q = q.replace("'", "\\'")
    return f"name like '{safe_q}'"


@app.get("/")
def root():
    return {
        "service": "Local VCF Operations for Networks API Proxy",
        "version": "1.5.0",
        "base_url": NI_BASE_URL,
        "verify_ssl": VERIFY_SSL
    }


@app.get("/ni/health")
def health():
    return {
        "status": "ok",
        "service": "vcf-networks-api-proxy",
        "version": "1.5.0",
        "target": NI_BASE_URL,
        "domain_type": NI_DOMAIN_TYPE,
        "auth_mode": "api-token"
    }


@app.post("/ni/login")
def login(body: LoginRequest):
    return client.login(body.username, body.password, body.domain_type)


@app.get("/ni/search")
def search_get(
    query: str = Query("vm", description="Simple name/IP text or full VCF Networks filter expression."),
    entity_type: str = Query("VirtualMachine", description="Entity type, for example VirtualMachine."),
    size: int = Query(50, description="Max number of results.")
):
    payload = {
        "entity_type": entity_type,
        "filter": build_search_filter(query),
        "size": size
    }

    return client.request("POST", "/api/ni/search", json=payload)


@app.post("/ni/search")
def search_post(body: SearchRequest):
    payload = {
        "entity_type": body.entity_type,
        "filter": body.filter,
        "size": body.size
    }

    return client.request("POST", "/api/ni/search", json=payload)


@app.get("/ni/entities/vms")
def list_vms(
    query: str = Query("vm", description="VM name/IP text or full filter expression."),
    size: int = Query(50, description="Max number of results.")
):
    payload = {
        "entity_type": "VirtualMachine",
        "filter": build_search_filter(query),
        "size": size
    }

    return client.request("POST", "/api/ni/search", json=payload)


@app.get("/ni/entities/nsx-segments")
def list_nsx_segments(
    query: str = Query("segment", description="Segment name text or full filter expression."),
    size: int = Query(50, description="Max number of results.")
):
    payload = {
        "entity_type": "NSXTLogicalSwitch",
        "filter": build_search_filter(query),
        "size": size
    }

    return client.request("POST", "/api/ni/search", json=payload)


@app.get("/ni/entities/nsx-t1")
def list_nsx_t1(
    query: str = Query("t1", description="Tier-1 name text or full filter expression."),
    size: int = Query(50, description="Max number of results.")
):
    payload = {
        "entity_type": "NSXTLogicalRouter",
        "filter": build_search_filter(query),
        "size": size
    }

    return client.request("POST", "/api/ni/search", json=payload)


@app.get("/ni/version")
def version():
    return client.request("GET", "/api/ni/info/version")


@app.get("/ni/infra/nodes")
def infra_nodes():
    return client.request("GET", "/api/ni/infra/nodes")


@app.get("/ni/data-sources/vcenters")
def vcenter_data_sources():
    return client.request("GET", "/api/ni/data-sources/vcenters")


def resolve_entity(name: str, entity_type: str = "VirtualMachine") -> Dict[str, Any]:
    """Resolve a name or IP to a Network Insight entity reference.

    The path API wants {entity_id, entity_type} objects, not names, so a
    lookup has to happen first. IP addresses are passed through untouched —
    the API accepts those directly and resolving them would be lossy.
    """
    text = name.strip()

    # Already an entity ID, e.g. 10000:1:4378167812621755938
    if re.fullmatch(r"\d+:\d+:\d+", text):
        return {"entity_id": text, "entity_type": entity_type}

    # An IP address: the API takes these as-is
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", text):
        return {"ip": text}

    result = client.request("POST", "/api/ni/search", json={
        "entity_type": entity_type,
        "filter": build_search_filter(text),
        "size": 5,
    })
    results = (result or {}).get("results") or []
    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"No {entity_type} found matching '{name}'",
        )
    return {"entity_id": results[0].get("entity_id"), "entity_type": entity_type}


@app.post("/ni/path")
def path_lookup(body: PathRequest):
    """Trace the network path between two entities.

    The upstream path is /api/ni/infra/path — not /api/ni/path — and it takes
    structured entity references rather than bare names, so both endpoints are
    resolved through search first.
    """
    payload = {
        "source": resolve_entity(body.source),
        "destination": resolve_entity(body.destination),
    }
    if body.port:
        payload["port"] = body.port
    if body.protocol:
        payload["protocol"] = body.protocol

    return {
        "request": payload,
        "path": client.request("POST", "/api/ni/infra/path", json=payload),
    }


@app.get("/ni/alerts")
def alerts():
    return client.request(
        "GET",
        "/api/ni/entities/problems"
    )

@app.get("/ni/alerts/{problem_id}")
def alert_details(problem_id: str):
    return client.request(
        "GET",
        f"/api/ni/entities/problems/{problem_id}"
    )

@app.get("/ni/vms")
def vms():
    return client.request(
        "GET",
        "/api/ni/entities/vms"
    )

@app.get("/ni/vms/{vm_id}")
def vm_details(vm_id: str):
    return client.request(
        "GET",
        f"/api/ni/entities/vms/{vm_id}"
    )
@app.get("/ni/hosts")
def hosts():
    return client.request("GET", "/api/ni/entities/hosts")
    
@app.get("/ni/hosts/{host_id}")
def host_details(host_id: str):
    return client.request("GET", f"/api/ni/entities/hosts/{host_id}")
    
@app.get("/ni/clusters")
def clusters():
    return client.request("GET", "/api/ni/entities/clusters")
    
@app.get("/ni/clusters/{cluster_id}")
def cluster_details(cluster_id: str):
    return client.request("GET", f"/api/ni/entities/clusters/{cluster_id}")

@app.get("/ni/flows")
def flows(
    source: Optional[str] = Query(None, description="Source VM name or IP."),
    destination: Optional[str] = Query(None, description="Destination VM name or IP."),
    port: Optional[str] = Query(None, description="Optional destination port."),
    protocol: Optional[str] = Query(None, description="Optional protocol TCP/UDP."),
    filter: Optional[str] = Query(None, description="Raw Network Insight filter expression, overrides the others."),
    hours: int = Query(24, description="Look back this many hours. Flow search defaults to a zero-width window, which always returns nothing."),
    size: int = Query(50, description="Max number of results.")
):
    """Traffic flows, via the Network Insight search DSL.

    Flows are not a REST collection in Network Insight; they are queried
    through /api/ni/search with entity_type Flow and a filter expression.
    Field naming in that DSL varies between versions, so `filter` is exposed
    as an escape hatch: if the generated expression is rejected, the caller
    can supply one that matches their appliance.
    """
    if filter:
        expression = filter
    else:
        clauses = []
        if source:
            clauses.append(f"source_vm.name = '{source.replace(chr(39), chr(92) + chr(39))}'")
        if destination:
            clauses.append(f"destination_vm.name = '{destination.replace(chr(39), chr(92) + chr(39))}'")
        if port:
            clauses.append(f"port = {port}")
        if protocol:
            clauses.append(f"protocol = '{protocol.upper()}'")
        if not clauses:
            raise HTTPException(
                status_code=400,
                detail="Provide at least one of source, destination, port, protocol, or a raw filter.",
            )
        expression = " and ".join(clauses)

    now = int(time.time())
    start = now - max(hours, 1) * 3600
    payload = {
        "entity_type": "Flow",
        "filter": expression,
        "size": size,
        "time_range": {"start_time": start, "end_time": now},
    }

    try:
        results = client.request("POST", "/api/ni/search", json=payload)
    except HTTPException as exc:
        # Surface the query that failed. A bare 400 from the appliance is
        # impossible to act on without seeing the expression that caused it.
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "message": "Flow search rejected by VCF Networks",
                "filter_used": expression,
                "upstream": exc.detail,
                "hint": "Field names in the flow DSL are version-specific. "
                        "Retry with an explicit ?filter= expression.",
            },
        )

    return {"filter_used": expression, "time_range": payload["time_range"], "results": results}
