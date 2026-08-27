"""Veeam Backup & Replication — read-only API wrapper.

Answers the question the other four systems cannot: is this VM actually backed
up, and when did it last succeed. vCenter can say a VM exists and VCF
Operations can say it is unhealthy; neither knows whether losing it would cost
you anything.

Two things about this API are worth knowing before reading the code:

  * Every request needs an x-api-version header, and it must match the build.
    Each release differs, and a mismatch returns an opaque 400 rather than
    saying so. The installed build here is 13.0.1.1071 on Windows, whose header
    value was not known when this was written, so the version is negotiated at
    login, reported in every response, and the rejection bodies are surfaced on
    failure — the server usually names the version it wants.

    On Windows the REST API is served by its own service, "Veeam Backup &
    Replication REST API Service", listening on 9419. It is not always running
    after an install or upgrade, and a stopped service presents as a connection
    refused rather than anything about Veeam.
  * A job reporting Success is not evidence a given VM is protected. Jobs
    succeed while silently skipping VMs. Protection is therefore derived from
    restore points, which are per-object facts.

Environment:
    VEEAM_URL           https://veeam01.vcf.local:9419
    VEEAM_USER          required
    VEEAM_PASSWORD      required
    VEEAM_API_VERSION   pin the header instead of negotiating
    VEEAM_VERIFY_SSL    default false
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Any, Dict, List, Optional
import os
import time
import datetime
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI(
    title="Veeam Backup & Replication API",
    version="1.0.0",
    description="Read-only wrapper for Veeam Backup & Replication",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

VEEAM_URL = os.getenv("VEEAM_URL", "https://veeam01.vcf.local:9419").rstrip("/")
VEEAM_USER = os.getenv("VEEAM_USER")
VEEAM_PASS = os.getenv("VEEAM_PASSWORD")
VERIFY_SSL = os.getenv("VEEAM_VERIFY_SSL", "false").lower() == "true"
TIMEOUT = int(os.getenv("VEEAM_TIMEOUT", "60"))

# Newest first. The installed build is Veeam 13, whose header value is not
# documented in this repo, so 13-era candidates lead and the 12.x values are
# kept as fallbacks. If none work the rejection bodies are returned, because
# Veeam normally names the version it expects.
CANDIDATE_VERSIONS = [
    "1.3-rev0",   # 13.0
    "1.2-rev1",   # 12.3
    "1.2-rev0",   # 12.2
    "1.1-rev1",   # 12.1
    "1.1-rev0",   # 12.0
]
PINNED_VERSION = os.getenv("VEEAM_API_VERSION")

_session: Dict[str, Any] = {"token": None, "expires_at": 0.0, "api_version": None}


def _require_credentials() -> None:
    missing = [n for n, v in (("VEEAM_USER", VEEAM_USER), ("VEEAM_PASSWORD", VEEAM_PASS)) if not v]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"{' and '.join(missing)} not set on the MCP server.",
        )


def _login(api_version: str):
    return requests.post(
        f"{VEEAM_URL}/api/oauth2/token",
        data={"grant_type": "password", "username": VEEAM_USER, "password": VEEAM_PASS},
        headers={
            "x-api-version": api_version,
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        verify=VERIFY_SSL,
        timeout=TIMEOUT,
    )


def authenticate(force: bool = False) -> Dict[str, Any]:
    """Log in, then work out which API version the server will serve data on.

    The version is deliberately not negotiated at the token endpoint. Probing
    veeam01 with a deliberately invalid user showed every candidate version
    returning 401: credentials are checked first, so the header is never
    reached and any value appears to be accepted. Negotiating there would
    always "succeed" on the first candidate and then fail on real calls —
    a check that cannot fail, which is worse than no check.

    So the token is obtained once, and the version is settled against an
    endpoint that actually returns data.
    """
    _require_credentials()
    if not force and _session["token"] and time.time() < _session["expires_at"]:
        return _session

    versions = [PINNED_VERSION] if PINNED_VERSION else CANDIDATE_VERSIONS

    try:
        response = _login(versions[0])
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach {VEEAM_URL}: {exc}. On Windows, check the "
                   f"'Veeam Backup Server RESTful API Service' is running — it "
                   f"is a separate service from the backup service itself, and "
                   f"is Delayed Start, so it lags a reboot by a minute or two.",
        )

    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail=f"Veeam rejected user '{VEEAM_USER}'. The version header is not "
                   f"checked at this endpoint, so this is a credential problem, "
                   f"not a version problem.",
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Veeam login failed: {response.text[:500]}",
        )

    body = response.json()
    _session.update({
        "token": body["access_token"],
        "expires_at": time.time() + max(int(body.get("expires_in", 900)) - 60, 60),
        "api_version": versions[0],
    })

    if not PINNED_VERSION:
        _session["api_version"] = _negotiate_version(_session["token"])

    return _session


def _negotiate_version(token: str) -> str:
    """Find a version the server will actually serve data on.

    Tried newest-first against a cheap read. Whichever value works is reported
    on every response, because it identifies the build and explains any
    endpoint that later 404s.
    """
    attempts = []
    for version in CANDIDATE_VERSIONS:
        try:
            probe = requests.get(
                f"{VEEAM_URL}/api/v1/serverInfo",
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-api-version": version,
                    "Accept": "application/json",
                },
                verify=VERIFY_SSL,
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            attempts.append(f"{version} -> {exc}")
            continue

        if probe.status_code == 200:
            return version
        attempts.append(f"{version} -> {probe.status_code}: {probe.text[:120]}")

    # Data calls will now carry the newest candidate and may fail, but saying
    # so is better than reporting a version that was never confirmed.
    _session["version_unconfirmed"] = attempts
    return CANDIDATE_VERSIONS[0]


def request(method: str, path: str, **kwargs) -> Any:
    """Call Veeam, re-authenticating once if the token expired."""
    def _call():
        session = authenticate()
        return requests.request(
            method,
            f"{VEEAM_URL}{path}",
            headers={
                "Authorization": f"Bearer {session['token']}",
                "x-api-version": session["api_version"],
                "Accept": "application/json",
            },
            verify=VERIFY_SSL,
            timeout=TIMEOUT,
            **kwargs,
        )

    response = _call()
    if response.status_code == 401:
        authenticate(force=True)
        response = _call()

    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Veeam {method} {path} failed (api-version "
                   f"{_session['api_version']}): {response.text[:500]}",
        )
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:2000]}


def _age_hours(timestamp: Optional[str]) -> Optional[float]:
    """Hours since an ISO 8601 timestamp, or None if it cannot be parsed."""
    if not timestamp:
        return None
    try:
        text = timestamp.replace("Z", "+00:00")
        when = datetime.datetime.fromisoformat(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - when
        return round(delta.total_seconds() / 3600, 1)
    except (ValueError, TypeError):
        return None


@app.get("/veeam/health")
def health(deep: bool = Query(False, description="Force a fresh login instead of reusing the cached token.")):
    """Authenticate for real, and report the negotiated API version.

    Reuses the cached token by default and only logs in when it has expired.
    The UI polls health every 30 seconds per open tab, and forcing a login
    each time would mean thousands of daily authentications by a backup
    server administrator account — noise that looks like an attack in an
    audit log, and a lockout risk if the password is ever rotated.

    An expired or rejected token still re-authenticates here and fails
    loudly, so "ok" continues to mean a usable session exists rather than
    merely that the port is open.
    """
    try:
        session = authenticate(force=deep)
    except HTTPException as exc:
        return {"status": "error", "veeam_url": VEEAM_URL, "detail": exc.detail}
    return {
        "status": "ok",
        "veeam_url": VEEAM_URL,
        "user": VEEAM_USER,
        "api_version": session["api_version"],
        "version_negotiated": PINNED_VERSION is None,
        "version_confirmed": "version_unconfirmed" not in session,
        "version_attempts": session.get("version_unconfirmed"),
        "authenticated": True,
        "checked": "fresh login" if deep else "cached token",
    }


@app.get("/veeam/jobs")
def jobs(limit: int = Query(100, ge=1, le=500)):
    """Configured backup jobs."""
    data = request("GET", "/api/v1/jobs", params={"limit": limit})
    return {"jobs": data.get("data", data), "api_version": _session["api_version"]}


@app.get("/veeam/sessions")
def sessions(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(200, ge=1, le=500),
    failed_only: bool = Query(False, description="Only sessions that did not succeed."),
):
    """Recent job runs, with their outcome."""
    data = request("GET", "/api/v1/sessions", params={"limit": limit})
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {"unexpected_shape": True, "raw": data}

    out = []
    for row in rows:
        age = _age_hours(row.get("endTime") or row.get("creationTime"))
        if age is not None and age > hours:
            continue
        result = ((row.get("result") or {}).get("result")
                  if isinstance(row.get("result"), dict) else row.get("result"))
        record = {
            "name": row.get("name"),
            "type": row.get("sessionType"),
            "state": row.get("state"),
            "result": result,
            "ended": row.get("endTime"),
            "age_hours": age,
        }
        if failed_only and str(result).lower() in ("success", "none", "null"):
            continue
        out.append(record)

    return {
        "window_hours": hours,
        "session_count": len(out),
        "failed_only": failed_only,
        "api_version": _session["api_version"],
        "sessions": out,
    }


@app.get("/veeam/protection/{vm_name}")
def protection(vm_name: str, stale_after_hours: int = Query(48, ge=1)):
    """Is this VM actually backed up, and how recently.

    Derived from restore points rather than job results. A job can report
    Success while skipping a VM, so job state is not evidence that any
    particular VM is recoverable — a restore point is.
    """
    data = request("GET", "/api/v1/objectRestorePoints",
                   params={"nameFilter": vm_name, "limit": 100})
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {"vm": vm_name, "unexpected_shape": True, "raw": data}

    points = []
    for row in rows:
        created = row.get("creationTime")
        points.append({
            "created": created,
            "age_hours": _age_hours(created),
            "name": row.get("name"),
            "platform": row.get("platformName"),
        })
    points.sort(key=lambda p: p["age_hours"] if p["age_hours"] is not None else 1e9)

    newest = points[0] if points else None
    age = newest["age_hours"] if newest else None
    protected = newest is not None
    stale = protected and age is not None and age > stale_after_hours

    if not protected:
        verdict = (f"No restore points found for '{vm_name}'. Either it is not "
                   f"protected by any job, or the name does not match Veeam's. "
                   f"Do not report it as backed up.")
    elif stale:
        verdict = (f"Newest restore point is {age}h old, over the "
                   f"{stale_after_hours}h threshold. Backups are configured but "
                   f"not current.")
    else:
        verdict = f"Protected. Newest restore point is {age}h old."

    return {
        "vm": vm_name,
        "protected": protected,
        "stale": stale,
        "newest_restore_point_age_hours": age,
        "restore_point_count": len(points),
        "threshold_hours": stale_after_hours,
        "verdict": verdict,
        "api_version": _session["api_version"],
        "restore_points": points[:10],
    }


@app.get("/veeam/unprotected")
def unprotected(limit: int = Query(200, ge=1, le=500)):
    """Objects Veeam knows about that have no restore points.

    Only covers what Veeam has seen. A VM absent from Veeam entirely will not
    appear here, so this cannot prove the estate is fully protected — compare
    against the vCenter inventory for that.
    """
    data = request("GET", "/api/v1/backupObjects", params={"limit": limit})
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {"unexpected_shape": True, "raw": data}

    out = [{"name": r.get("name"), "type": r.get("type"), "platform": r.get("platformName")}
           for r in rows if not r.get("restorePointsCount")]
    return {
        "objects_known_to_veeam": len(rows),
        "without_restore_points": len(out),
        "caveat": ("Covers only objects Veeam knows about. A VM never added to a "
                   "job does not appear here at all; cross-check the vCenter "
                   "inventory before calling the estate protected."),
        "api_version": _session["api_version"],
        "objects": out,
    }


@app.get("/veeam/version")
def version():
    """What Veeam build this actually is.

    /api/v1/serverInfo is the same endpoint the api-version negotiation
    settles against, so it is known to answer. Field names are read off the
    payload rather than assumed: whatever Veeam calls them, the full response
    is returned alongside, so an unexpected shape is visible instead of
    silently producing nulls.
    """
    data = request("GET", "/api/v1/serverInfo")
    if not isinstance(data, dict):
        return {"unexpected_shape": True, "raw": data}

    known = {
        "name": data.get("name"),
        "build_version": data.get("buildVersion"),
        "patch_level": data.get("patchLevel"),
        "database_vendor": data.get("databaseVendor"),
        "platform": data.get("platform"),
    }
    # Surfaced rather than left in raw: an unregistered or unpatched backup
    # server is worth saying out loud when someone asks what is running.
    patches = data.get("patches")
    if isinstance(patches, list):
        known["patches_applied"] = len(patches)
    registration = data.get("veeamRegistration")
    if isinstance(registration, dict) and "isRegistered" in registration:
        known["registered"] = registration["isRegistered"]

    return {
        "product": "Veeam Backup & Replication",
        **{k: v for k, v in known.items() if v is not None and v != ""},
        "rest_api_version": _session["api_version"],
        "api_version_confirmed": "version_unconfirmed" not in _session,
        "fields_returned_by_server": sorted(data),
        "raw": data,
    }


@app.get("/veeam/raw")
def raw(path: str = Query(..., description="Path under the Veeam API, e.g. /api/v1/jobs"),
        limit: int = Query(5, ge=1, le=50)):
    """Passthrough for discovering the real response shape."""
    data = request("GET", path, params={"limit": limit})
    return {
        "path": path,
        "api_version": _session["api_version"],
        "top_level_keys": sorted(data) if isinstance(data, dict) else None,
        "response": data,
    }
