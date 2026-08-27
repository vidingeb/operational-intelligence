import os
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor

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
    """Report whether Network Insight can actually be reached and authenticated.

    This returned a hardcoded "ok" while the credentials behind it had been
    overwritten with placeholders, so nothing detected the outage. Returns 200
    either way; check `status`, not the HTTP code.
    """
    info = {
        "service": "vcf-networks-api-proxy",
        "version": "1.6.0",
        "target": NI_BASE_URL,
        "domain_type": NI_DOMAIN_TYPE,
        "auth_mode": "api-token",
        "credentials_configured": bool(NI_USERNAME and NI_PASSWORD),
    }
    try:
        upstream = client.request("GET", "/api/ni/info/version")
        info["status"] = "ok"
        if isinstance(upstream, dict):
            info["upstream_version"] = upstream.get("version")
    except HTTPException as exc:
        info["status"] = "unavailable"
        info["error"] = exc.detail
    except Exception as exc:
        info["status"] = "unavailable"
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


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


IP_RE = re.compile(r"\d{1,3}(\.\d{1,3}){3}")


def resolve_ip(name: str) -> str:
    """Resolve a VM name to an IP address.

    The documented Network Insight endpoints that take endpoints take IP
    addresses, not entity references, so a name has to be turned into an IP
    before it is useful. An IP is returned untouched.
    """
    text = name.strip()

    if IP_RE.fullmatch(text):
        return text

    result = client.request("POST", "/api/ni/search", json={
        "entity_type": "VirtualMachine",
        "filter": build_search_filter(text),
        "size": 5,
    })
    results = (result or {}).get("results") or []
    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"No VirtualMachine found matching '{name}'",
        )

    entity_id = results[0].get("entity_id")
    detail = client.request("GET", f"/api/ni/entities/vms/{quote(str(entity_id), safe='')}")

    # ip_addresses is a list of objects: [{"ip_address": "10.0.0.100", ...}]
    for entry in detail.get("ip_addresses") or []:
        if isinstance(entry, dict) and entry.get("ip_address"):
            return entry["ip_address"]
        if isinstance(entry, str) and IP_RE.fullmatch(entry):
            return entry

    raise HTTPException(
        status_code=422,
        detail={
            "message": f"Resolved '{name}' to entity {entity_id} but it has no IP address recorded",
            "hint": "Pass an IP address directly.",
        },
    )


@app.post("/ni/path")
def path_lookup(body: PathRequest):
    """Firewall rules applying between two endpoints.

    Network Insight does NOT expose a public hop-by-hop path or topology API.
    The UI's Path Visualization uses a private internal route. The only
    documented Path operation is /api/ni/path/firewall-rules, which answers
    "what firewall rules govern traffic between A and B" rather than "what
    route does it take". The response says so explicitly, so a caller is not
    misled into reading it as a topology trace.
    """
    source_ip = resolve_ip(body.source)
    destination_ip = resolve_ip(body.destination)

    payload: Dict[str, Any] = {
        "source_ip_address": source_ip,
        "destination_ip_address": destination_ip,
    }
    if body.port:
        payload["port"] = int(body.port)
    if body.protocol:
        payload["protocol"] = body.protocol.upper()

    rules = client.request("POST", "/api/ni/path/firewall-rules", json=payload)

    return {
        "note": "Network Insight has no public topology/path-trace API. "
                "These are the firewall rules applying between the two endpoints, "
                "not a hop-by-hop network path.",
        "resolved": {
            "source": {"input": body.source, "ip": source_ip},
            "destination": {"input": body.destination, "ip": destination_ip},
        },
        "firewall_rules": rules,
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

@app.get("/ni/vms/inventory")
def vms_inventory(
    limit: int = Query(50, ge=1, le=200, description="Maximum VMs to return"),
    vlan: Optional[str] = Query(None, description="Only VMs on this L2 network, e.g. vlan-1000"),
):
    """List VMs with their IPs and port groups in a single call.

    Listing VMs returns entity references only, so answering "IP and port
    group per VM" previously meant one detail call per VM. The model ran out
    of tool rounds after three and presented those three as a sample, which
    reads like an answer but is not one.

    Fetching the details here turns an unbounded number of round trips into
    one, so the model gets the whole picture and can report a real total.
    """
    listing = client.request("GET", "/api/ni/entities/vms")
    refs = (listing or {}).get("results") or []
    total_known = (listing or {}).get("total_count", len(refs))

    def fetch(ref):
        entity_id = ref.get("entity_id")
        if not entity_id:
            return None
        try:
            return client.request(
                "GET", f"/api/ni/entities/vms/{quote(str(entity_id), safe='')}"
            )
        except HTTPException:
            return {"entity_id": entity_id, "error": "could not fetch VM detail"}

    with ThreadPoolExecutor(max_workers=8) as pool:
        details = list(pool.map(fetch, refs[:limit]))

    vms_out = []
    for detail in details:
        if not detail:
            continue
        if detail.get("error"):
            vms_out.append(detail)
            continue

        ips = []
        for entry in detail.get("ip_addresses") or []:
            if isinstance(entry, dict) and entry.get("ip_address"):
                ips.append(entry["ip_address"])
            elif isinstance(entry, str):
                ips.append(entry)

        # Link-local addresses are noise on every VM and mean nothing to an
        # operator; the model previously reported them as a column of their
        # own. Keep them only when a VM has nothing else.
        routable = [ip for ip in ips if not ip.lower().startswith("fe80:")]
        if routable:
            ips = sorted(routable, key=lambda ip: (":" in ip, ip))

        networks = []
        for entry in detail.get("layer2_networks") or []:
            if isinstance(entry, dict):
                label = entry.get("entity_name") or entry.get("name")
                if label:
                    networks.append(label)
            elif isinstance(entry, str):
                networks.append(entry)

        record = {
            "entity_id": detail.get("entity_id"),
            "name": detail.get("name") or detail.get("entity_name"),
            "ip_addresses": ips,
            "port_groups": networks,
        }
        for key, source in (("host", "host"), ("cluster", "cluster"), ("vcenter", "vcenter_manager")):
            value = detail.get(source)
            if isinstance(value, dict) and value.get("entity_name"):
                record[key] = value["entity_name"]
        if detail.get("power_state"):
            record["power_state"] = detail["power_state"]
        vms_out.append(record)

    if vlan:
        wanted = vlan.strip().lower()
        vms_out = [v for v in vms_out if any(wanted == n.lower() for n in v.get("port_groups", []))]

    return {
        "vm_count": len(vms_out),
        "total_vms_known": total_known,
        # Say so explicitly: a silently truncated list reads like a full one.
        "truncated": len(refs) > limit,
        "hint": (
            f"Showing {limit} of {len(refs)} VMs. Call again with limit={min(len(refs), 200)} "
            "for the complete list."
        ) if len(refs) > limit else None,
        "filter": {"vlan": vlan} if vlan else None,
        "vms": vms_out,
    }


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

MAX_HYDRATE = 20


def hydrate_flows(results: Dict[str, Any], size: int) -> list:
    """Turn flow entity references into readable flow records.

    Search only returns {entity_id, entity_type, time}, which tells a caller
    nothing about the traffic. Each reference is fetched and flattened into
    the fields people actually ask about: who talked to whom, on what port,
    and whether the firewall allowed it. Capped so a broad query cannot fan
    out into hundreds of upstream calls.
    """
    refs = (results or {}).get("results") or []
    out = []

    for ref in refs[:min(size, MAX_HYDRATE)]:
        entity_id = ref.get("entity_id")
        if not entity_id:
            continue
        try:
            flow = client.request(
                "GET",
                f"/api/ni/entities/flows/{quote(str(entity_id), safe='')}",
            )
        except HTTPException:
            out.append({"entity_id": entity_id, "error": "could not fetch flow detail"})
            continue

        port = flow.get("port") or {}
        out.append({
            "entity_id": entity_id,
            "name": flow.get("name"),
            "source_ip": (flow.get("source_ip") or {}).get("ip_address"),
            "destination_ip": (flow.get("destination_ip") or {}).get("ip_address"),
            "port": port.get("display") or port.get("start"),
            "protocol": flow.get("protocol"),
            "traffic_type": flow.get("traffic_type"),
            "firewall_action": flow.get("firewall_action"),
            "source_host": (flow.get("source_host") or {}).get("entity_name"),
            "destination_host": (flow.get("destination_host") or {}).get("entity_name"),
            "source_l2_network": (flow.get("source_l2_network") or {}).get("entity_name"),
        })

    if len(refs) > len(out):
        out.append({
            "note": f"{len(refs)} flows matched, {len(out)} shown. "
                    f"Narrow the filter or reduce the time window for more detail."
        })
    return out


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
            clauses.append(f"source_ip.ip_address = '{resolve_ip(source)}'")
        if destination:
            clauses.append(f"destination_ip.ip_address = '{resolve_ip(destination)}'")
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

    return {
        "filter_used": expression,
        "time_range": payload["time_range"],
        "flow_count": (results or {}).get("total_count", 0),
        "flows": hydrate_flows(results, size),
    }


@app.get("/ni/flows/recent")
def flows_recent(
    hours: int = Query(1, description="Look back this many hours."),
    size: int = Query(50, description="Max number of results.")
):
    """Recently observed flows, unfiltered.

    Uses the documented /api/ni/entities/flows collection. Unlike the search
    based /ni/flows this takes no source or destination filter, so it is the
    reliable way to answer "is flow data being collected at all".
    """
    now = int(time.time())
    return client.request(
        "GET",
        f"/api/ni/entities/flows?size={size}"
        f"&start_time={now - max(hours, 1) * 3600}&end_time={now}",
    )


@app.get("/ni/flows/detail/{flow_id}")
def flow_detail(flow_id: str):
    """A single flow entity, with its real field names.

    Useful for working out what the flow search DSL will accept: the filter
    fields mirror the entity's own structure, and that structure varies
    between Network Insight versions.
    """
    return client.request(
        "GET",
        f"/api/ni/entities/flows/{quote(flow_id, safe='')}",
    )
