"""Web API contract: reads return JSON, writes land as pending proposals, and
the confirm button is the only path from proposal to committed row."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session
from starlette.testclient import TestClient

import applyr.llm.say as say_mod
from applyr.config import Config
from applyr.core.enums import Status
from applyr.llm.provider import ProviderError, ToolCallRequest
from applyr.web.server import create_app
from tests.conftest import pipeline
from tests.fakes import FakeProvider


@pytest.fixture()
def client(engine: Engine, config: Config) -> Iterator[TestClient]:
    app = create_app(config, engine)
    with TestClient(app) as c:
        yield c


def seed(engine: Engine) -> None:
    with Session(engine) as session:
        pipeline(
            session,
            company="Stripe",
            title="Backend Engineer",
            statuses=[(Status.SAVED, 10), (Status.APPLIED, 8), (Status.SCREENING, 3)],
        )
        session.commit()


# --- static + reads ----------------------------------------------------------


def test_index_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Applyr" in response.text


def test_applications_empty(client: TestClient) -> None:
    assert client.get("/api/applications").json() == []


def test_applications_lists_seeded(client: TestClient, engine: Engine) -> None:
    seed(engine)
    rows = client.get("/api/applications").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["ref"] == "app#1"
    assert row["status"] == "screening"
    assert row["company"] == "Stripe"
    assert row["title"] == "Backend Engineer"
    # tracker-table fields are always present, even when unset
    assert {"comp_min", "comp_max", "location", "saved_at", "applied_at", "priority"} <= row.keys()


def test_overview_shape(client: TestClient, engine: Engine) -> None:
    seed(engine)
    data = client.get("/api/overview").json()
    assert {"briefing", "funnel"} <= data.keys()
    assert {"interviews_next_48h", "tasks_due", "stale", "pending_proposals"} <= data[
        "briefing"
    ].keys()
    reached = {s["stage"]: s["reached"] for s in data["funnel"]}
    assert reached.get("applied") == 1


def test_stats_shape(client: TestClient, engine: Engine) -> None:
    seed(engine)
    data = client.get("/api/stats").json()
    assert {"funnel", "by_resume", "by_source", "by_time_to_apply"} <= data.keys()


# --- dispatch: the propose→confirm invariant over HTTP -----------------------


def test_dispatch_write_creates_pending_proposal_not_a_row(
    client: TestClient, engine: Engine
) -> None:
    seed(engine)
    res = client.post(
        "/api/dispatch",
        json={"name": "add_note", "args": {"application": "app#1", "text": "hello"}},
    ).json()
    assert res["result"] == "proposal_created"
    assert res["proposal"]["kind"] == "add_note"

    # not committed yet: show has no notes, the proposal sits pending
    shown = client.post("/api/dispatch", json={"name": "show", "args": {"ref": "app#1"}}).json()
    assert shown["data"]["notes"] == []
    pending = client.post(
        "/api/dispatch", json={"name": "list_proposals", "args": {"status": "pending"}}
    ).json()
    assert [p["id"] for p in pending["data"]["proposals"]] == [res["proposal"]["id"]]


def test_confirm_commits_and_reject_discards(client: TestClient, engine: Engine) -> None:
    seed(engine)
    first = client.post(
        "/api/dispatch",
        json={"name": "add_note", "args": {"application": "app#1", "text": "keep me"}},
    ).json()
    second = client.post(
        "/api/dispatch",
        json={"name": "add_note", "args": {"application": "app#1", "text": "drop me"}},
    ).json()

    confirmed = client.post(
        "/api/dispatch",
        json={"name": "confirm_proposal", "args": {"proposal_id": first["proposal"]["id"]}},
    ).json()
    assert confirmed["result"] == "ok"
    rejected = client.post(
        "/api/dispatch",
        json={"name": "reject_proposal", "args": {"proposal_id": second["proposal"]["id"]}},
    ).json()
    assert rejected["result"] == "ok"

    shown = client.post("/api/dispatch", json={"name": "show", "args": {"ref": "app#1"}}).json()
    assert [n["text"] for n in shown["data"]["notes"]] == ["keep me"]


def test_dispatch_unknown_tool_is_error(client: TestClient) -> None:
    res = client.post("/api/dispatch", json={"name": "drop_tables", "args": {}}).json()
    assert res["result"] == "error"


def test_dispatch_rejects_malformed_body(client: TestClient) -> None:
    assert client.post("/api/dispatch", content=b"not json").status_code == 400
    assert client.post("/api/dispatch", json={"name": 42, "args": {}}).status_code == 400


# --- say ---------------------------------------------------------------------


def test_say_plans_one_tool_call(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(engine)
    fake = FakeProvider(
        tool_calls=[
            ToolCallRequest(name="add_note", arguments={"application": "app#1", "text": "hi"})
        ]
    )
    monkeypatch.setattr(say_mod, "provider_for", lambda config, task: fake)
    res = client.post("/api/say", json={"text": "note on stripe: hi"}).json()
    assert res["result"] == "proposal_created"
    assert res["tool"] == "add_note"  # echoed so the UI can retry after disambiguation
    assert res["args"]["text"] == "hi"


def test_say_plain_text_reply(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeProvider(chat_responses=["that is a question, not a capture"])
    monkeypatch.setattr(say_mod, "provider_for", lambda config, task: fake)
    res = client.post("/api/say", json={"text": "how are you"}).json()
    assert res == {"result": "ok", "reply": "that is a question, not a capture"}


def test_say_provider_down_is_clean_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(config: Config, task: str) -> FakeProvider:
        raise ProviderError("ollama unreachable")

    monkeypatch.setattr(say_mod, "provider_for", boom)
    response = client.post("/api/say", json={"text": "applied to stripe"})
    assert response.status_code == 502
    assert response.json() == {"result": "error", "message": "ollama unreachable"}


def test_say_requires_text(client: TestClient) -> None:
    assert client.post("/api/say", json={"text": "  "}).status_code == 400
