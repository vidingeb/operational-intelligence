"""Conversation persistence and the sidebar.

The chat lost its thread after a reload. Server memory was never the problem —
it replayed history correctly when given an id — but the id lived in a bare JS
variable, so a refresh silently started a new conversation while the page still
looked like the same session. The comment above that variable claimed a reload
"picks the thread back up"; it never did.

These tests cover both halves: the id survives, and the sidebar the user
navigates with is wired to elements that actually exist.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import web_ui  # noqa: E402

esprima = pytest.importorskip("esprima")


def _script() -> str:
    """The evaluated page's JavaScript, as the browser would receive it."""
    scripts = re.findall(r"<script>(.*?)</script>", web_ui.HTML_PAGE, re.S)
    assert scripts, "no <script> block found in the page"
    return "\n".join(scripts)


# --- the wiring that kills the whole pane when it is wrong ------------------

def test_every_element_the_script_looks_up_exists_in_the_page():
    """A getElementById returning null throws and takes the entire script with
    it, so the pane dies wholesale rather than losing one feature."""
    page = web_ui.HTML_PAGE
    ids = set(re.findall(r"getElementById\('([^']+)'\)", _script()))
    missing = [i for i in ids if f'id="{i}"' not in page]
    assert not missing, f"script looks up ids that the page never defines: {missing}"


def test_the_script_still_parses():
    esprima.parseScript(_script())


def test_the_sidebar_markup_is_present():
    page = web_ui.HTML_PAGE
    for marker in ('id="sidebar"', 'id="conv-list"', 'id="sidebar-new"',
                   'id="workspace"', 'id="main"'):
        assert marker in page, f"missing {marker}"


def test_the_chat_pane_is_inside_the_workspace_row():
    """If the sidebar is not a sibling of the chat column it renders above the
    conversation instead of beside it."""
    page = web_ui.HTML_PAGE
    assert page.index('id="workspace"') < page.index('id="sidebar"')
    assert page.index('id="sidebar"') < page.index('id="chat-container"')
    assert page.index('id="chat-container"') < page.index('id="input-area"')


# --- persistence ------------------------------------------------------------

def test_the_conversation_id_is_written_to_local_storage():
    script = _script()
    assert "localStorage.setItem(CONV_KEY" in script
    assert "localStorage.removeItem(CONV_KEY)" in script


def test_the_page_restores_the_thread_on_load():
    """The whole bug: nothing read the id back."""
    script = _script()
    assert "restoreConversation();" in script, \
        "the page must reload the stored conversation at startup"
    assert "storedConversationId()" in script


def test_no_path_assigns_the_id_without_persisting_it():
    """A stray `conversationId = ...` reintroduces the bug for that path only,
    which is the hardest version to notice."""
    script = _script()
    stray = re.findall(r"(?<![.\w])(?<!let )conversationId\s*=\s*", script)
    # Only the assignment inside setConversationId is legitimate; the `let`
    # declaration is excluded above.
    assert len(stray) == 1, \
        f"conversationId is assigned outside setConversationId ({len(stray)} sites)"


def test_local_storage_failure_does_not_break_the_chat():
    """Private browsing throws on setItem; that must cost memory, not the pane."""
    script = _script()
    setter = script[script.index("function setConversationId"):
                    script.index("function storedConversationId")]
    assert "try {" in setter and "catch" in setter


def test_new_chat_clears_the_stored_id():
    script = _script()
    body = script[script.index("function newConversation"):
                  script.index("// --- conversation sidebar")]
    assert "setConversationId(null)" in body, \
        "New chat must clear persistence, or the old thread returns on reload"


def test_a_deleted_conversation_is_dropped_rather_than_retried():
    """A remembered id whose conversation is gone would otherwise send every
    question into a thread the server does not have."""
    script = _script()
    assert "response.status === 404" in script
    assert "setConversationId(null)" in script


# --- the proxy endpoints ----------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    return TestClient(web_ui.app)


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected {self.status_code}")


def _stub(monkeypatch, handler):
    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            return handler("GET", url, params)

        async def delete(self, url):
            return handler("DELETE", url, None)

    monkeypatch.setattr(web_ui.httpx, "AsyncClient", FakeClient)


def test_the_list_is_proxied(client, monkeypatch):
    _stub(monkeypatch, lambda m, url, p: _Response(
        200, {"conversations": [{"id": "abc", "title": "Which VMs?", "messages": 4}]}))

    body = client.get("/api/conversations").json()

    assert body["conversations"][0]["title"] == "Which VMs?"


def test_a_missing_conversation_returns_404_not_a_blank_500(client, monkeypatch):
    """The page keys its recovery off this status; a 500 would strand a stale id."""
    _stub(monkeypatch, lambda m, url, p: _Response(404, {"detail": "gone"}))

    response = client.get("/api/conversations/nope")

    assert response.status_code == 404
    assert response.json()["detail"] == "No such conversation"


def test_history_is_returned_in_order(client, monkeypatch):
    messages = [{"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"}]
    _stub(monkeypatch, lambda m, url, p: _Response(
        200, {"conversation_id": "abc", "messages": messages}))

    body = client.get("/api/conversations/abc").json()

    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_delete_is_proxied(client, monkeypatch):
    seen = {}

    def handler(method, url, params):
        seen["method"] = method
        seen["url"] = url
        return _Response(200, {"deleted": "abc"})

    _stub(monkeypatch, handler)
    client.delete("/api/conversations/abc")

    assert seen["method"] == "DELETE"
    assert seen["url"].endswith("/conversations/abc")
