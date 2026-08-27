"""Conversation memory, schedule timing, and the read-only guarantee.

Three things are worth proving here, because all three fail quietly:

  - due-time arithmetic, where an off-by-one means a daily report fires twice
    or not at all and nobody notices for a week;
  - restart behaviour, where a scheduler that replays every missed slot turns
    two days of downtime into forty tool-calling runs against production;
  - the read-only guarantee, because an unattended job that can change state is
    the one bug in here that damages something.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import schedule_times as st  # noqa: E402
import store  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "state.db")
    store.init_db(path)
    return path


def utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# --- schedule timing ----------------------------------------------------------

def test_daily_before_the_slot_runs_today():
    due = st.next_due("daily", 7, 0, after=utc(2026, 8, 28, 3, 0))
    assert due == utc(2026, 8, 28, 7, 0)


def test_daily_after_the_slot_waits_for_tomorrow():
    due = st.next_due("daily", 7, 0, after=utc(2026, 8, 28, 9, 0))
    assert due == utc(2026, 8, 29, 7, 0)


def test_daily_exactly_at_the_slot_moves_on():
    """Strictly after, or a job that just ran is due again and loops."""
    due = st.next_due("daily", 7, 0, after=utc(2026, 8, 28, 7, 0))
    assert due == utc(2026, 8, 29, 7, 0)


def test_hourly_rolls_into_the_next_hour():
    due = st.next_due("hourly", 0, 30, after=utc(2026, 8, 28, 9, 45))
    assert due == utc(2026, 8, 28, 10, 30)


def test_weekly_on_its_own_weekday_before_the_slot_runs_today():
    # 2026-08-28 is a Friday (weekday 4).
    assert utc(2026, 8, 28).weekday() == 4
    due = st.next_due("weekly", 7, 0, weekday=4, after=utc(2026, 8, 28, 3, 0))
    assert due == utc(2026, 8, 28, 7, 0)


def test_weekly_on_its_own_weekday_after_the_slot_waits_a_week():
    due = st.next_due("weekly", 7, 0, weekday=4, after=utc(2026, 8, 28, 9, 0))
    assert due == utc(2026, 9, 4, 7, 0)


def test_weekly_crosses_the_month_boundary():
    due = st.next_due("weekly", 7, 0, weekday=0, after=utc(2026, 8, 28, 9, 0))
    assert due == utc(2026, 8, 31, 7, 0)


def test_a_missed_window_fires_once_not_once_per_missed_day():
    """Two days of downtime must not release two days of backlog."""
    after_outage = st.catch_up("daily", 7, 0, now=utc(2026, 8, 30, 12, 0))
    assert after_outage == utc(2026, 8, 31, 7, 0)


@pytest.mark.parametrize("kind,hour,minute,weekday", [
    ("daily", 25, 0, None),
    ("daily", 7, 61, None),
    ("weekly", 7, 0, None),
    ("weekly", 7, 0, 9),
    ("yearly", 7, 0, None),
])
def test_impossible_schedules_are_rejected_not_stored(kind, hour, minute, weekday):
    """A schedule with hour=25 looks fine in a list and never fires."""
    with pytest.raises(ValueError):
        st.next_due(kind, hour, minute, weekday)


def test_descriptions_read_back():
    assert st.describe("daily", 7, 0) == "daily at 07:00 UTC"
    assert st.describe("hourly", 0, 5) == "every hour at :05"
    assert st.describe("weekly", 7, 30, 0) == "every Monday at 07:30 UTC"


# --- conversation memory ------------------------------------------------------

def test_history_returns_turns_oldest_first(db):
    cid = store.create_conversation(path=db)
    store.add_message(cid, "user", "what VMs are running?", path=db)
    store.add_message(cid, "assistant", "63 VMs.", path=db)
    store.add_message(cid, "user", "which are powered off?", path=db)
    store.add_message(cid, "assistant", "28.", path=db)
    got = store.history(cid, limit_turns=6, path=db)
    assert [m["content"] for m in got] == [
        "what VMs are running?", "63 VMs.", "which are powered off?", "28."]


def test_history_is_capped_and_keeps_the_most_recent(db):
    """Uncapped history plus 12k-token answers evicts the actual question."""
    cid = store.create_conversation(path=db)
    for n in range(10):
        store.add_message(cid, "user", f"q{n}", path=db)
        store.add_message(cid, "assistant", f"a{n}", path=db)
    got = store.history(cid, limit_turns=2, path=db)
    assert [m["content"] for m in got] == ["q8", "a8", "q9", "a9"]


def test_history_of_an_unknown_conversation_is_empty_not_an_error(db):
    assert store.history("nope", path=db) == []


def test_conversations_are_isolated(db):
    first = store.create_conversation(path=db)
    second = store.create_conversation(path=db)
    store.add_message(first, "user", "about vCenter", path=db)
    store.add_message(second, "user", "about Veeam", path=db)
    assert [m["content"] for m in store.history(first, path=db)] == ["about vCenter"]
    assert [m["content"] for m in store.history(second, path=db)] == ["about Veeam"]


def test_sequence_numbers_do_not_collide(db):
    cid = store.create_conversation(path=db)
    for n in range(50):
        store.add_message(cid, "user", f"m{n}", path=db)
    assert len(store.history(cid, limit_turns=100, path=db)) == 50


def test_a_conversation_is_titled_by_its_first_question(db):
    cid = store.create_conversation(path=db)
    store.add_message(cid, "user", "which datastores are low on space?", path=db)
    store.add_message(cid, "user", "and which hosts?", path=db)
    titles = [c["title"] for c in store.list_conversations(path=db) if c["id"] == cid]
    assert titles == ["which datastores are low on space?"]


def test_deleting_a_conversation_removes_its_messages(db):
    cid = store.create_conversation(path=db)
    store.add_message(cid, "user", "secret question", path=db)
    store.delete_conversation(cid, path=db)
    assert store.history(cid, path=db) == []
    assert all(c["id"] != cid for c in store.list_conversations(path=db))


# --- schedules and runs -------------------------------------------------------

def test_a_schedule_survives_being_written_and_read(db):
    sid = store.create_schedule("which VMs have no restore point?", "daily", 7, 0,
                                next_run="2026-08-29T07:00:00+00:00", path=db)
    got = store.get_schedule(sid, path=db)
    assert got["question"] == "which VMs have no restore point?"
    assert got["enabled"] == 1
    assert got["next_run"] == "2026-08-29T07:00:00+00:00"


def test_only_due_and_enabled_schedules_are_returned(db):
    due = store.create_schedule("due", "daily", 7, 0,
                                next_run="2026-08-28T07:00:00+00:00", path=db)
    store.create_schedule("later", "daily", 9, 0,
                          next_run="2026-08-29T09:00:00+00:00", path=db)
    paused = store.create_schedule("paused", "daily", 7, 0,
                                   next_run="2026-08-28T07:00:00+00:00", path=db)
    store.set_schedule_enabled(paused, False, path=db)
    ids = [s["id"] for s in
           store.due_schedules("2026-08-28T08:00:00+00:00", path=db)]
    assert ids == [due]


def test_marking_a_run_moves_the_schedule_forward(db):
    sid = store.create_schedule("q", "daily", 7, 0,
                                next_run="2026-08-28T07:00:00+00:00", path=db)
    store.mark_schedule_ran(sid, "2026-08-29T07:00:00+00:00", path=db)
    got = store.get_schedule(sid, path=db)
    assert got["next_run"] == "2026-08-29T07:00:00+00:00"
    assert got["last_run"] is not None
    assert not store.due_schedules("2026-08-28T08:00:00+00:00", path=db)


def test_a_failed_run_is_recorded_rather_than_lost(db):
    """A schedule that quietly stopped producing reports is the bad case."""
    rid = store.start_run("q", schedule_id="s1", path=db)
    store.finish_run(rid, error="ConnectError: Ollama unreachable", path=db)
    run = store.get_run(rid, path=db)
    assert run["status"] == "error"
    assert "Ollama unreachable" in run["error"]
    assert run["finished_at"] is not None


def test_previous_answer_ignores_failed_runs(db):
    """Continuity must compare against the last real answer, not a failure."""
    good = store.start_run("q", schedule_id="s1", path=db)
    store.finish_run(good, answer="52 VMs behind", path=db)
    bad = store.start_run("q", schedule_id="s1", path=db)
    store.finish_run(bad, error="timeout", path=db)
    assert store.previous_answer("s1", path=db)["answer"] == "52 VMs behind"


def test_previous_answer_is_none_on_the_first_run(db):
    assert store.previous_answer("brand-new", path=db) is None


def test_runs_can_be_listed_per_schedule(db):
    a = store.start_run("q1", schedule_id="s1", path=db)
    store.finish_run(a, answer="one", path=db)
    b = store.start_run("q2", schedule_id="s2", path=db)
    store.finish_run(b, answer="two", path=db)
    assert [r["id"] for r in store.list_runs(schedule_id="s1", path=db)] == [a]


def test_state_survives_reopening_the_database(db):
    """The service restarts; schedules must not restart with it."""
    sid = store.create_schedule("q", "daily", 7, 0,
                                next_run="2026-08-29T07:00:00+00:00", path=db)
    store.init_db(db)  # as a fresh process would
    assert store.get_schedule(sid, path=db)["question"] == "q"


# --- the read-only guarantee for unattended runs ------------------------------
#
# Exercised against the real chat_with_tools with a fake Ollama, because the
# claim "scheduled runs cannot change state" is only worth anything if the code
# path actually refuses, not if a flag is set somewhere.

import asyncio  # noqa: E402
import json  # noqa: E402

import orchestrator as o  # noqa: E402


class FakeOllama:
    """Stands in for the model. Round 1 calls a write tool; round 2 answers."""

    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.offered_tools = []
        self.round = 0

    async def post(self, url, json=None, **kwargs):
        self.offered_tools.append([t["function"]["name"]
                                   for t in (json or {}).get("tools", [])])
        self.round += 1
        if self.round == 1 and (json or {}).get("tools"):
            message = {"role": "assistant", "tool_calls": [
                {"function": {"name": self.tool_name, "arguments": {"vm": "vc01"}}}]}
        else:
            message = {"role": "assistant", "content": "Done."}
        return FakeResponse({"message": message})


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, fake):
        self._fake = fake

    async def __aenter__(self):
        return self._fake

    async def __aexit__(self, *args):
        return False


def _first_write_tool():
    for spec in o.REGISTRY:
        if spec["write"]:
            return spec["name"]
    return None


def test_the_registry_still_contains_write_tools():
    """Guards the two tests below from passing vacuously."""
    assert _first_write_tool() is not None, \
        "no write tools in the registry - the read-only tests prove nothing"


def test_a_scheduled_run_is_not_even_offered_write_tools(monkeypatch):
    write_tool = _first_write_tool()
    fake = FakeOllama(write_tool)
    monkeypatch.setattr(o.httpx, "AsyncClient", lambda **kw: FakeClient(fake))
    monkeypatch.setattr(o, "TOOL_SPECS", {t["name"]: t for t in o.REGISTRY})
    monkeypatch.setattr(o, "TOOLS_BY_SCOPE",
                        {"all": [o._schema(t) for t in o.REGISTRY]})

    asyncio.run(o.chat_with_tools("run the daily report", scope="all",
                                  read_only=True))
    offered = [names for names in fake.offered_tools if names]
    assert offered, "the model was never offered any tools"
    for names in offered:
        assert write_tool not in names, \
            f"{write_tool} was offered to an unattended run"


def test_a_write_call_is_refused_even_if_the_model_invents_it(monkeypatch):
    """Withholding schemas is not a control: a model can name a tool anyway."""
    write_tool = _first_write_tool()
    fake = FakeOllama(write_tool)
    executed = []

    async def spy(name, args, **kwargs):
        executed.append(name)
        return {"ok": True}

    monkeypatch.setattr(o.httpx, "AsyncClient", lambda **kw: FakeClient(fake))
    monkeypatch.setattr(o, "call_api", spy)
    monkeypatch.setattr(o, "TOOL_SPECS", {t["name"]: t for t in o.REGISTRY})
    monkeypatch.setattr(o, "TOOLS_BY_SCOPE",
                        {"all": [o._schema(t) for t in o.REGISTRY]})

    result = asyncio.run(o.chat_with_tools("run the daily report", scope="all",
                                           read_only=True))
    assert executed == [], f"a scheduled run executed {executed}"
    assert result["answer"]


def test_an_interactive_run_still_gets_its_tools(monkeypatch):
    """The guard must not quietly disarm normal use."""
    fake = FakeOllama("vcenter_vms")
    monkeypatch.setattr(o.httpx, "AsyncClient", lambda **kw: FakeClient(fake))
    monkeypatch.setattr(o, "TOOL_SPECS", {t["name"]: t for t in o.REGISTRY})
    monkeypatch.setattr(o, "TOOLS_BY_SCOPE",
                        {"all": [o._schema(t) for t in o.REGISTRY]})

    async def spy(name, args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(o, "call_api", spy)
    asyncio.run(o.chat_with_tools("what VMs are running?", scope="all"))
    assert any(names for names in fake.offered_tools), \
        "tools were withheld from an interactive question"


# --- API surface --------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A real app instance against a temporary database.

    SCHEDULER_ENABLED is off: a background loop would start firing questions at
    five production APIs during the test run.
    """
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setattr(o, "SCHEDULER_ENABLED", False)
    with TestClient(o.app) as c:
        yield c


def test_the_app_starts_with_the_scheduler_lifespan(client):
    """Catches a lifespan that raises, which would take the service down."""
    assert client.get("/schedules").status_code == 200


def test_creating_a_schedule_returns_its_next_run(client):
    response = client.post("/schedules", json={
        "question": "which VMs have no recent restore point?",
        "kind": "daily", "hour": 7, "minute": 0})
    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "daily at 07:00 UTC"
    assert body["next_run"] > store.utcnow(), "next run must be in the future"


def test_an_impossible_schedule_is_rejected_by_the_api(client):
    response = client.post("/schedules", json={
        "question": "q", "kind": "daily", "hour": 99, "minute": 0})
    assert response.status_code == 400
    assert "hour" in response.json()["detail"]


def test_an_empty_question_is_rejected(client):
    response = client.post("/schedules", json={"question": "   ", "kind": "daily"})
    assert response.status_code == 400


def test_an_unknown_scope_is_rejected(client):
    response = client.post("/schedules", json={
        "question": "q", "kind": "daily", "scope": "vcentre"})
    assert response.status_code == 400


def test_a_schedule_can_be_listed_then_deleted(client):
    sid = client.post("/schedules", json={
        "question": "daily estate report", "kind": "daily",
        "hour": 6, "minute": 30}).json()["id"]
    listed = client.get("/schedules").json()["schedules"]
    assert [s["id"] for s in listed] == [sid]
    assert listed[0]["description"] == "daily at 06:30 UTC"

    assert client.delete(f"/schedules/{sid}").status_code == 200
    assert client.get("/schedules").json()["schedules"] == []


def test_deleting_a_missing_schedule_is_a_404_not_a_silent_success(client):
    assert client.delete("/schedules/nope").status_code == 404


def test_reports_start_empty_and_an_unknown_one_is_404(client):
    assert client.get("/runs").json()["runs"] == []
    assert client.get("/runs/nope").status_code == 404


# --- memory through the chat endpoint -----------------------------------------

@pytest.fixture
def chat_client(tmp_path, monkeypatch):
    """The chat endpoint with the model and Ollama stubbed out.

    Records the conversation it was handed, so a test can assert what the model
    would actually have seen rather than what we hoped it would see.
    """
    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setattr(o, "SCHEDULER_ENABLED", False)

    async def no_model_check():
        return set()

    seen = []

    async def fake_chat(message, model=None, conversation=None, scope="all",
                        read_only=False):
        seen.append(list(conversation or []))
        return {"answer": f"answer to {message}", "usage": {},
                "tools_called": [], "pending_actions": []}

    async def no_telemetry():
        return {}

    monkeypatch.setattr(o, "_installed_models", no_model_check)
    monkeypatch.setattr(o, "chat_with_tools", fake_chat)
    monkeypatch.setattr(o, "fetch_telemetry", no_telemetry)
    with TestClient(o.app) as c:
        yield c, seen


def test_a_first_question_starts_a_conversation(chat_client):
    client, seen = chat_client
    body = client.post("/chat", json={"message": "what VMs are running?"}).json()
    assert body["conversation_id"]
    assert body["history_turns"] == 0
    assert [m["role"] for m in seen[0]] == ["system"], \
        "a first question must not replay anything"


def test_a_follow_up_replays_the_earlier_turns(chat_client):
    client, seen = chat_client
    first = client.post("/chat", json={"message": "what VMs are running?"}).json()
    second = client.post("/chat", json={
        "message": "which of those are powered off?",
        "conversation_id": first["conversation_id"]}).json()

    assert second["conversation_id"] == first["conversation_id"]
    assert second["history_turns"] == 1
    replayed = [m["content"] for m in seen[1] if m["role"] != "system"]
    assert replayed == ["what VMs are running?",
                        "answer to what VMs are running?"], \
        "the follow-up could not know what 'those' referred to"


def test_omitting_the_id_starts_a_clean_thread(chat_client):
    """What the New chat button relies on."""
    client, seen = chat_client
    client.post("/chat", json={"message": "first"})
    body = client.post("/chat", json={"message": "unrelated"}).json()
    assert body["history_turns"] == 0
    assert [m["role"] for m in seen[1]] == ["system"]


def test_replayed_history_is_capped(chat_client, monkeypatch):
    client, seen = chat_client
    monkeypatch.setattr(o, "HISTORY_TURNS", 2)
    cid = client.post("/chat", json={"message": "q0"}).json()["conversation_id"]
    for n in range(1, 5):
        client.post("/chat", json={"message": f"q{n}", "conversation_id": cid})
    replayed = [m["content"] for m in seen[-1] if m["role"] != "system"]
    assert replayed == ["q2", "answer to q2", "q3", "answer to q3"]


def test_tool_output_is_never_replayed(chat_client):
    """One estate answer is 12k tokens of JSON; three would evict the question."""
    client, seen = chat_client
    cid = client.post("/chat", json={"message": "first"}).json()["conversation_id"]
    client.post("/chat", json={"message": "second", "conversation_id": cid})
    assert all(m["role"] in ("system", "user", "assistant") for m in seen[1])


# --- Regression: the chat 500 hunt ---------------------------------------

def test_store_works_without_an_explicit_init(tmp_path):
    """Using the store before init_db must not raise "no such table".

    The live chat endpoint returned a bare 500 and the first reproduction was
    exactly this error. Depending on a startup hook to create the schema makes
    every other entry point a latent failure.
    """
    db = str(tmp_path / "fresh.db")
    store._INITIALISED.discard(db)
    cid = store.create_conversation(title="no init called", path=db)
    store.add_message(cid, "user", "hello", path=db)
    assert [m["content"] for m in store.history(cid, path=db)] == ["hello"]


def test_connections_are_closed(tmp_path):
    """Each operation must close its connection, not just commit it.

    ``with sqlite3.connect(...)`` commits and leaves the handle open, so a
    long-lived service leaks one descriptor per request until it cannot open
    any more.
    """
    db = str(tmp_path / "fds.db")
    store.init_db(db)
    opened = []
    real = store.sqlite3.connect

    def tracking(*a, **k):
        conn = real(*a, **k)
        opened.append(conn)
        return conn

    store.sqlite3.connect = tracking
    try:
        for i in range(5):
            store.create_conversation(title=f"c{i}", path=db)
    finally:
        store.sqlite3.connect = real

    assert len(opened) == 5
    for conn in opened:
        with pytest.raises(store.sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
