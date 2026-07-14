from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import Session

from jobtrack.config import Config
from jobtrack.core.enums import Status
from jobtrack.llm.provider import ToolCallRequest
from jobtrack.llm.say import plan, run_call
from jobtrack.llm.tools import ToolContext
from tests.conftest import pipeline
from tests.fakes import FakeProvider


def _ctx(session: Session, engine: Engine, config: Config, provider: FakeProvider) -> ToolContext:
    config.llm.tasks["say"] = "local"
    ctx = ToolContext(session=session, engine=engine, config=config, source="say")
    return ctx


def test_plan_emits_a_tool_call(
    session: Session, engine: Engine, config: Config, monkeypatch: object
) -> None:
    provider = FakeProvider(
        tool_calls=[
            ToolCallRequest(
                name="update_status",
                arguments={"application": "Stripe", "to_status": "screening"},
            )
        ]
    )
    import jobtrack.llm.say as say_mod

    def fake_provider_for(cfg: Config, task: str) -> FakeProvider:
        return provider

    monkeypatch.setattr(say_mod, "provider_for", fake_provider_for)  # type: ignore[attr-defined]

    pipeline(session, statuses=[(Status.APPLIED, 5)])
    ctx = ToolContext(session=session, engine=engine, config=config, source="say")
    response = plan(ctx, "I heard back from Stripe")
    assert response.tool_calls
    call = response.tool_calls[0]
    assert call.name == "update_status"

    result = run_call(ctx, call)
    assert result.result == "proposal_created"
    assert result.proposal is not None
    # not auto-approvable: status change always confirms
    assert not result.proposal.auto_approvable
