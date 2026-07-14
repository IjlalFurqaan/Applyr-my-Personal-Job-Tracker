from __future__ import annotations

import inspect
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jobtrack.config import Config
from jobtrack.core.enums import Status
from jobtrack.core.events import derived_status
from jobtrack.core.models import Company
from jobtrack.llm.tools import TOOLS, ToolContext, dispatch, tool_schemas
from tests.conftest import add_status, make_app, make_company, make_job, pipeline


def ctx_for(session: Session, engine: Engine, config: Config) -> ToolContext:
    return ToolContext(session=session, engine=engine, config=config, source="mcp")


def test_add_job_with_unknown_company_nests_creation(
    session: Session, engine: Engine, config: Config
) -> None:
    ctx = ctx_for(session, engine, config)
    result = dispatch(ctx, "add_job", {"company": "Stripe", "title": "Backend Engineer"})
    assert result.result == "proposal_created"
    assert result.proposal is not None
    assert "[NEW]" in result.proposal.diff
    assert not result.proposal.auto_approvable
    confirm = dispatch(ctx, "confirm_proposal", {"proposal_id": result.proposal.id})
    assert confirm.result == "ok"
    assert session.exec(select(Company)).first() is not None


def test_disambiguation_round_trip(session: Session, engine: Engine, config: Config) -> None:
    co = make_company(session, "Stripe")
    app1 = make_app(session, make_job(session, co, "Backend Engineer"))
    app2 = make_app(session, make_job(session, co, "Data Scientist"))
    add_status(session, app1, Status.APPLIED, days_ago=5)
    add_status(session, app2, Status.APPLIED, days_ago=3)
    ctx = ctx_for(session, engine, config)

    first = dispatch(ctx, "update_status", {"application": "Stripe", "to_status": "screening"})
    assert first.result == "needs_disambiguation"
    assert first.data is not None and first.data["disambiguate_param"] == "application"
    assert first.candidates is not None and len(first.candidates) == 2

    chosen = first.candidates[0].ref  # ranked by most-recent activity
    second = dispatch(
        ctx, "update_status", {"application": chosen, "to_status": "screening"}
    )
    assert second.result == "proposal_created"
    assert second.proposal is not None
    confirmed = dispatch(ctx, "confirm_proposal", {"proposal_id": second.proposal.id})
    assert confirmed.result == "ok"
    chosen_id = int(chosen.split("#")[1])
    assert derived_status(session, chosen_id) is Status.SCREENING
    # the other application is untouched
    other_id = app2.id if chosen_id == app1.id else app1.id
    assert derived_status(session, other_id or 0) is Status.APPLIED


def test_add_note_is_auto_approvable_update_status_is_not(
    session: Session, engine: Engine, config: Config
) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 2)])
    ctx = ctx_for(session, engine, config)
    note = dispatch(ctx, "add_note", {"application": f"app#{app.id}", "text": "hi"})
    assert note.proposal is not None and note.proposal.auto_approvable
    move = dispatch(
        ctx, "update_status", {"application": f"app#{app.id}", "to_status": "screening"}
    )
    assert move.proposal is not None and not move.proposal.auto_approvable


def test_validation_error_is_a_clean_error_result(
    session: Session, engine: Engine, config: Config
) -> None:
    ctx = ctx_for(session, engine, config)
    result = dispatch(ctx, "update_status", {})
    assert result.result == "error"
    assert result.message is not None and "invalid arguments" in result.message


def test_unknown_tool(session: Session, engine: Engine, config: Config) -> None:
    result = dispatch(ctx_for(session, engine, config), "drop_database", {})
    assert result.result == "error"


def test_search_finds_application_fuzzily(
    session: Session, engine: Engine, config: Config
) -> None:
    pipeline(session, statuses=[(Status.APPLIED, 1)])
    ctx = ctx_for(session, engine, config)
    result = dispatch(ctx, "search", {"query": "stripe"})
    assert result.result == "ok"
    assert result.data is not None
    refs = [h["ref"] for h in result.data["hits"]]
    assert any(r.startswith("app#") or r.startswith("co#") for r in refs)


def test_show_application_detail(session: Session, engine: Engine, config: Config) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 4), (Status.SCREENING, 1)])
    ctx = ctx_for(session, engine, config)
    result = dispatch(ctx, "show", {"ref": f"app#{app.id}"})
    assert result.result == "ok"
    assert result.data is not None
    assert result.data["status"] == "screening"
    assert len(result.data["events"]) == 2


def test_reject_proposal_via_tool(session: Session, engine: Engine, config: Config) -> None:
    ctx = ctx_for(session, engine, config)
    created = dispatch(ctx, "add_job", {"company": "Acme", "title": "Engineer"})
    assert created.proposal is not None
    rejected = dispatch(ctx, "reject_proposal", {"proposal_id": created.proposal.id})
    assert rejected.result == "ok"
    assert session.exec(select(Company)).first() is None
    again = dispatch(ctx, "confirm_proposal", {"proposal_id": created.proposal.id})
    assert again.result == "error"


def test_tool_schemas_are_well_formed() -> None:
    schemas = tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == set(TOOLS)
    for schema in schemas:
        assert schema["function"]["description"]
        assert isinstance(schema["function"]["parameters"], dict)


def test_mcp_wrappers_mirror_param_models() -> None:
    """The MCP server's explicit wrappers must not drift from the registry."""
    from jobtrack.mcp import server

    for name, spec in TOOLS.items():
        tool_obj: Any = getattr(server, name)
        fn = getattr(tool_obj, "fn", tool_obj)
        wrapper_params = set(inspect.signature(fn).parameters)
        model_params = set(spec.params_model.model_fields)
        assert wrapper_params == model_params, f"{name}: {wrapper_params} != {model_params}"
