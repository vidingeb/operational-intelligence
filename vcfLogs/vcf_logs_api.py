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
# The constraint grammar was established against log01 rather than from
# documentation, because four plausible-looking forms were rejected first:
#
#   timestamp>VALUE          -> 400 missing_argument
#   timestamp/>/VALUE        -> 400 missing_argument (value read as a field)
#   timestamp>=VALUE         -> 400 missing_argument
#   timestamp/GT/VALUE       -> 400 missing_argument
#   timestamp/>VALUE         -> 200
#
# So a constraint is "field" / "OPERATORVALUE" — the field is its own path
# segment, and the operator is glued to the front of the value. Multiple
# constraints chain with further "/" pairs, which was confirmed rather than
# assumed: timestamp/>X/text/CONTAINS error returns 200.
#
# The operator and value are percent-encoded; the separating "/" is not.
#
# Operators are not interchangeable across types. "=" was rejected on every
# string field tried — text, source, facility and priority all returned
# invalid_constraints — so string matching is CONTAINS only, and ">" is for
# the numeric timestamp. This is why priority is matched with CONTAINS below
# even though equality would express the intent better.

FIELD_TEXT = "text"
FIELD_HOSTNAME = "hostname"
FIELD_PRIORITY = "priority"


def _segment(field: str, operator: str, value: Any) -> str:
    return f"{field}/" + requests.utils.quote(f"{operator}{value}", safe="")


def _constraints(hours: int, contains: Optional[str] = None,
                 field: str = FIELD_TEXT,
                 priority: Optional[str] = None) -> str:
    """Build the constraint path. Echoed on every response so a wrong query is
    visible in the answer rather than looking like an absence of logs."""
    start_ms = int((time.time() - max(hours, 1) * 3600) * 1000)
    parts = [_segment("timestamp", ">", start_ms)]
    if contains:
        parts.append(_segment(field, "CONTAINS ", contains))
    if priority:
        parts.append(_segment(FIELD_PRIORITY, "CONTAINS ", priority))
    return "/".join(parts)


def _resolve_fields(event: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an event's "fields" list into a plain mapping.

    Two shapes appear in the same list, which is the part worth knowing:
    some fields carry their value in "content" (source, priority, facility),
    while others are positional and give "startPosition"/"length" as offsets
    into "text" (hostname, appname, procid). Reading only "content" would
    silently lose the hostname, which is the field most worth having.
    """
    text = event.get("text") or ""
    resolved: Dict[str, Any] = {}
    for entry in event.get("fields") or []:
        name = entry.get("name")
        if not name:
            continue
        if "content" in entry:
            resolved[name] = entry["content"]
        elif "startPosition" in entry and "length" in entry:
            start = entry["startPosition"]
            resolved[name] = text[start:start + entry["length"]]
    return resolved


def _shape(event: Dict[str, Any]) -> Dict[str, Any]:
    """Present one event with the fields an engineer actually reads."""
    fields = _resolve_fields(event)
    return {
        "time": event.get("timestampString"),
        "timestamp": event.get("timestamp"),
        "host": fields.get("hostname") or fields.get("source"),
        "app": fields.get("appname"),
        "priority": fields.get("priority"),
        "facility": fields.get("facility"),
        "text": event.get("text"),
    }


def _events(hours: int, limit: int, contains: Optional[str] = None,
            field: str = FIELD_TEXT,
            priority: Optional[str] = None) -> Dict[str, Any]:
    limit = min(max(limit, 1), MAX_LIMIT)
    constraints = _constraints(hours, contains, field, priority)
    data = request("GET", f"/api/v2/events/{constraints}", params={"limit": limit})

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
        "search_complete": data.get("complete"),
        "truncated": len(events) >= limit,
        "hint": (f"Returned the first {limit} matching events; there may be more. "
                 "Say so rather than presenting this as every occurrence.")
                if len(events) >= limit else None,
        "events": [_shape(e) for e in events],
    }


@app.get("/logs/health")
def health(deep: bool = Query(False, description="Force a fresh login instead of reusing the cached session.")):
    """Authenticate for real, so 'ok' means the credentials work.

    By default this reuses the cached session and only logs in when it has
    expired. That still proves a usable session exists, because an expired or
    rejected one re-authenticates here and fails loudly.

    It deliberately does not force a login every time. The UI polls health
    every 30 seconds per open tab, and log01 issues a new 1800-second session
    per login with no logout, so forcing would leave thousands of sessions
    accumulating daily. Pass deep=true when you want the credentials
    themselves re-checked.
    """
    try:
        get_token(force=deep)
    except HTTPException as exc:
        return {"status": "error", "logs_url": LOGS_URL, "detail": exc.detail}
    return {
        "status": "ok",
        "logs_url": LOGS_URL,
        "user": LOGS_USER,
        "provider": LOGS_PROVIDER,
        "authenticated": True,
        "checked": "fresh login" if deep else "cached session",
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
    priority: str = Query("err", description="Syslog priority, e.g. err, crit, alert, emerg."),
):
    """Recent error-level log activity, filtered by syslog priority.

    Filtering on priority rather than searching the text for "error" matters:
    the priority is what the sending daemon declared, whereas the word "error"
    appears in plenty of informational messages and is missing from plenty of
    real failures.

    The value is a syslog priority, so it is "err" and not "error". Matching
    is CONTAINS rather than equality because log01 rejects "=" on string
    fields; in practice the priority values are distinct enough that a
    substring of one does not match another.

    Note this returns only the one priority. It is not "everything bad" —
    warning, crit and alert are separate values, so ask for them explicitly.
    """
    result = _events(hours, limit, priority=priority)
    result["filtered_by"] = f"priority={priority}"
    return result


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


@app.get("/logs/version")
def version():
    """VCF Operations for Logs product version.

    /api/v2/version answered 401 unauthenticated during discovery, which is
    how we know it exists — with a session token it should return the build.
    The whole payload is included because these field names have not been
    seen yet and should not be guessed at.
    """
    data = request("GET", "/api/v2/version")
    if not isinstance(data, dict):
        return {"unexpected_shape": True, "raw": data}
    known = {
        "version": data.get("version"),
        "release_name": data.get("releaseName"),
        "build": data.get("build") or data.get("buildNumber"),
    }
    out = {
        "product": "VMware VCF Operations for Logs (Aria Operations for Logs)",
        **{k: v for k, v in known.items() if v is not None},
        "fields_returned_by_server": sorted(data),
        "raw": data,
    }
    # This appliance reports releaseName "Nightly". A pre-GA build collecting
    # the estate's logs is worth stating, not burying in the version string.
    release = (known.get("release_name") or "").strip().lower()
    if release and release not in ("ga", "release", "general availability"):
        out["build_type_note"] = (
            f"Release name is '{known['release_name']}', not a GA release. "
            "Worth noting if these logs are relied on for audit or support."
        )
    return out


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
