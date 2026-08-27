"""VCF Operations for Logs (Aria Operations for Logs) — read-only API wrapper.

Runs on the MCP server alongside the vCenter, VCF Operations and VCF Networks
wrappers, and follows the same rules learned from those three:

  * /logs/health authenticates for real. A health check that returns a
    hardcoded "ok" tells you a process is listening and nothing else, which is
    how VCF Operations went months without a working password.
  * Query construction is echoed back on every response, so a rejection is
    diagnosable rather than an opaque 4xx.
  * /logs/raw exists so the real response shape can be read off the live system
    instead of inferred. Every field name guessed in this codebase so far has
    needed correcting against reality.

Environment:
    LOGS_URL           https://log01.vcf.local
    LOGS_USER          default admin
    LOGS_PASSWORD      required
    LOGS_PROVIDER      Local (default) or ActiveDirectory
    LOGS_VERIFY_SSL    default false
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Any, Dict, Optional
import os
import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(
    title="VCF Operations for Logs API",
    version="1.0.0",
    description="Read-only wrapper for VCF Operations for Logs",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

LOGS_URL = os.getenv("LOGS_URL", "https://log01.vcf.local:9543").rstrip("/")
LOGS_USER = os.getenv("LOGS_USER", "admin")
LOGS_PASS = os.getenv("LOGS_PASSWORD")
LOGS_PROVIDER = os.getenv("LOGS_PROVIDER", "Local")
VERIFY_SSL = os.getenv("LOGS_VERIFY_SSL", "false").lower() == "true"
TIMEOUT = int(os.getenv("LOGS_TIMEOUT", "60"))

MAX_LIMIT = 500

_session: Dict[str, Any] = {"token": None, "expires_at": 0.0}


def _require_password() -> None:
    if not LOGS_PASS:
        raise HTTPException(
            status_code=500,
            detail="LOGS_PASSWORD environment variable is not set on the MCP server.",
        )


def get_token(force: bool = False) -> str:
    """Session token, cached until shortly before it expires.

    Tokens are reused rather than acquired per request: the vCenter wrapper
    opened a new session on every call and exhausted the server's session
    limit, which presented as "works after a restart, dies later".
    """
    _require_password()
    if not force and _session["token"] and time.time() < _session["expires_at"]:
        return _session["token"]

    url = f"{LOGS_URL}/api/v2/sessions"
    payload = {"username": LOGS_USER, "password": LOGS_PASS, "provider": LOGS_PROVIDER}
    try:
        response = requests.post(
            url, json=payload, verify=VERIFY_SSL, timeout=TIMEOUT,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach {LOGS_URL}: {exc}")

    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail=f"Logs server rejected user '{LOGS_USER}' with provider "
                   f"'{LOGS_PROVIDER}'. If this is a domain account set "
                   f"LOGS_PROVIDER=ActiveDirectory.",
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Log session request failed: {response.text[:500]}",
        )

    body = response.json()
    token = body.get("sessionId") or body.get("token")
    if not token:
        raise HTTPException(
            status_code=502,
            detail=f"Log server returned no session id. Keys present: {sorted(body)}",
        )

    ttl = body.get("ttl") or 1800
    _session["token"] = token
    _session["expires_at"] = time.time() + max(int(ttl) - 60, 60)
    return token


def request(method: str, path: str, **kwargs) -> Any:
    """Call the logs API, retrying once if the cached session went stale."""
    def _call(token: str):
        return requests.request(
            method,
            f"{LOGS_URL}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            verify=VERIFY_SSL,
            timeout=TIMEOUT,
            **kwargs,
        )

    response = _call(get_token())
    if response.status_code == 401:
        response = _call(get_token(force=True))

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Logs API {method} {path} failed: {response.text[:500]}",
        )
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:2000]}


# --- Query construction ------------------------------------------------------
#
# The v2 API takes constraints as path segments, "field OPERATOR value", joined
# by "/". Constructing that inline made failures unreadable, so it is built in
# one place and echoed on every response.

def _constraints(hours: int, contains: Optional[str] = None,
                 field: str = "text") -> str:
    start_ms = int((time.time() - max(hours, 1) * 3600) * 1000)
    parts = [f"timestamp>{start_ms}"]
    if contains:
        parts.append(f"{field}/CONTAINS {contains}")
    return "/".join(parts)


def _events(hours: int, limit: int, contains: Optional[str] = None,
            field: str = "text") -> Dict[str, Any]:
    limit = min(max(limit, 1), MAX_LIMIT)
    constraints = _constraints(hours, contains, field)
    path = f"/api/v2/events/{requests.utils.quote(constraints, safe='/><=')}"
    data = request("GET", path, params={"limit": limit})

    events = data.get("events") if isinstance(data, dict) else None
    if events is None:
        # Shape not as expected — hand back what arrived rather than an empty
        # list, which would read as "no logs found".
        return {
            "query": constraints,
            "hours": hours,
            "limit": limit,
            "unexpected_shape": True,
            "keys_returned": sorted(data) if isinstance(data, dict) else type(data).__name__,
            "raw": data,
        }

    return {
        "query": constraints,
        "hours": hours,
        "limit": limit,
        "event_count": len(events),
        "truncated": len(events) >= limit,
        "hint": (f"Returned the first {limit} matching events; there may be more. "
                 "Say so rather than presenting this as every occurrence.")
                if len(events) >= limit else None,
        "events": events,
    }


@app.get("/logs/health")
def health():
    """Authenticate for real, so 'ok' means the credentials work."""
    try:
        get_token(force=True)
    except HTTPException as exc:
        return {"status": "error", "logs_url": LOGS_URL, "detail": exc.detail}
    return {
        "status": "ok",
        "logs_url": LOGS_URL,
        "user": LOGS_USER,
        "provider": LOGS_PROVIDER,
        "authenticated": True,
    }


@app.get("/logs/search")
def search(
    contains: Optional[str] = Query(None, description="Substring to match in the log text."),
    hours: int = Query(1, ge=1, le=168, description="Look back this many hours."),
    limit: int = Query(100, ge=1, le=MAX_LIMIT, description="Maximum events."),
):
    """Free-text log search across everything the log server has ingested."""
    return _events(hours, limit, contains)


@app.get("/logs/errors")
def errors(
    hours: int = Query(1, ge=1, le=168),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
):
    """Recent error-level log activity."""
    return _events(hours, limit, "error")


@app.get("/logs/for/{name}")
def logs_for(
    name: str,
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
):
    """Log events mentioning a named object — a VM, host, datastore or service.

    Matching is a substring search over the log text, so it finds messages that
    name the object without depending on which field the source populated.
    """
    result = _events(hours, limit, name)
    result["searched_for"] = name
    result["matching"] = "substring of the log message text"
    return result


@app.get("/logs/raw")
def raw(
    path: str = Query(..., description="Path under the logs API, e.g. /api/v2/events/timestamp>0"),
    limit: int = Query(5, ge=1, le=50),
):
    """Passthrough for discovering the real response shape.

    Present deliberately: every field name inferred in this codebase has
    needed correcting against a live payload.
    """
    data = request("GET", path, params={"limit": limit})
    return {
        "path": path,
        "top_level_keys": sorted(data) if isinstance(data, dict) else None,
        "response": data,
    }
