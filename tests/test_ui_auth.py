"""The UI's identity check.

These tests exist because header-based authentication has one classic failure:
the app is reachable by a path that does not go through the proxy, so the
attacker simply sets the header themselves. Verified against a real
`tailscale serve` before writing any of this:

  - a forged Tailscale-User-Login sent *through* the proxy is overwritten
    with the caller's real identity
  - the same header sent *directly* to the app port arrives untouched
  - the proxy connects from 127.0.0.1

So the rule under test is both halves together: a valid header AND a loopback
peer. Either alone is not authentication.
"""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator"))
import web_ui  # noqa: E402

PROXY = ("127.0.0.1", 51000)          # how tailscale serve appears
LAN = ("10.0.0.55", 51000)            # a machine on the datacenter LAN
IDENT = {"Tailscale-User-Login": "vidingeb@github",
         "Tailscale-User-Name": "vidingeb"}


@pytest.fixture(autouse=True)
def default_auth(monkeypatch):
    """Each test starts from the shipped defaults."""
    monkeypatch.setattr(web_ui, "UI_AUTH", "tailscale")
    monkeypatch.setattr(web_ui, "UI_ALLOWED_LOGINS", frozenset())


def client(peer=PROXY):
    return TestClient(web_ui.app, client=peer)


# --- the two halves of the rule -----------------------------------------------

def test_identity_header_from_the_proxy_is_accepted():
    res = client().get("/api/whoami", headers=IDENT)
    assert res.status_code == 200
    assert res.json()["login"] == "vidingeb@github"


def test_request_without_an_identity_header_is_refused():
    res = client().get("/api/whoami")
    assert res.status_code == 403
    assert "identity" in res.json()["detail"].lower()


def test_forged_header_from_off_box_is_refused():
    """The whole point: a LAN peer setting the header itself gets nothing."""
    res = client(peer=LAN).get("/api/whoami", headers=IDENT)
    assert res.status_code == 403
    assert "10.0.0.55" in res.json()["detail"]


def test_a_rewritten_peer_address_is_diagnosed_not_silently_refused():
    """Uvicorn rewrites the peer from X-Forwarded-For unless told not to.

    Behind a real `tailscale serve` that turns 127.0.0.1 into the caller's
    tailnet address, which refuses every legitimate request. Verified against
    live serve: client_host became 100.121.49.46 with default uvicorn flags.
    The request must still fail -- a forwarded address proves nothing -- but
    the reason has to point at the misconfiguration.
    """
    res = client(peer=("100.121.49.46", 51000)).get(
        "/api/whoami", headers={**IDENT, "X-Forwarded-For": "100.121.49.46"})
    assert res.status_code == 403
    assert "proxy headers" in res.json()["detail"].lower()


def test_a_forwarded_header_cannot_launder_a_lan_address():
    """Sending X-Forwarded-For must not turn a remote peer into a trusted one."""
    res = client(peer=LAN).get(
        "/api/whoami", headers={**IDENT, "X-Forwarded-For": "127.0.0.1"})
    assert res.status_code == 403


def test_the_page_itself_is_protected_not_just_the_api():
    """An unauthenticated GET / must not return the app."""
    assert client(peer=LAN).get("/").status_code == 403
    assert client().get("/").status_code == 403


def test_every_proxy_route_is_covered_by_the_middleware():
    """Auth is not applied route by route, so a new endpoint cannot forget it."""
    unauthenticated = client()
    for path in ("/", "/api/whoami", "/api/models", "/api/conversations",
                 "/api/schedules", "/api/memory", "/api/telemetry"):
        assert unauthenticated.get(path).status_code == 403, path


# --- allow-list ---------------------------------------------------------------

def test_allow_list_permits_a_named_login(monkeypatch):
    monkeypatch.setattr(web_ui, "UI_ALLOWED_LOGINS", frozenset({"vidingeb@github"}))
    assert client().get("/api/whoami", headers=IDENT).status_code == 200


def test_allow_list_refuses_another_tailnet_user(monkeypatch):
    monkeypatch.setattr(web_ui, "UI_ALLOWED_LOGINS", frozenset({"someone@else"}))
    res = client().get("/api/whoami", headers=IDENT)
    assert res.status_code == 403
    assert "vidingeb@github" in res.json()["detail"]


def test_login_comparison_ignores_case(monkeypatch):
    monkeypatch.setattr(web_ui, "UI_ALLOWED_LOGINS", frozenset({"vidingeb@github"}))
    shouty = {"Tailscale-User-Login": "VidInGeb@GitHub"}
    assert client().get("/api/whoami", headers=shouty).status_code == 200


def test_blank_header_is_not_an_identity():
    """An empty header must fail closed, not authenticate as ''."""
    res = client().get("/api/whoami", headers={"Tailscale-User-Login": "   "})
    assert res.status_code == 403


# --- the escape hatch ---------------------------------------------------------

def test_auth_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.setattr(web_ui, "UI_AUTH", "none")
    res = client(peer=LAN).get("/api/whoami")
    assert res.status_code == 200
    assert res.json()["auth"] == "none"


def test_disabling_auth_requires_the_exact_word(monkeypatch):
    """A typo in UI_AUTH must fail closed, not open."""
    monkeypatch.setattr(web_ui, "UI_AUTH", "non")
    assert client().get("/api/whoami").status_code == 403


# --- defaults -----------------------------------------------------------------

def test_defaults_are_locked_down():
    """A fresh import with no environment must not be open to the world."""
    fresh = importlib.reload(web_ui)
    try:
        assert fresh.UI_AUTH == "tailscale"
        assert fresh.UI_BIND == "127.0.0.1"
        assert fresh.UI_ALLOWED_LOGINS == frozenset()
    finally:
        importlib.reload(web_ui)


def test_ui_shows_the_identity_the_server_reports():
    """The header bar element the script writes into must exist in the page."""
    assert 'id="whoami"' in web_ui.HTML_PAGE
    assert "loadWhoami()" in web_ui.HTML_PAGE
