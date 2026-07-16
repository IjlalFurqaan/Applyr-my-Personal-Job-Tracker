"""Starlette app over the tool registry — the web sibling of mcp/server.py.

Three endpoint groups:
- aggregated reads the dashboard needs (/api/overview, /api/applications,
  /api/stats) — these compose core queries the same way the CLI does;
- /api/dispatch — the universal pipe into llm/tools.py, same contract as MCP
  (writes come back as pending proposals, never as committed rows);
- /api/say — natural-language capture: the local model picks one tool call,
  everything after that is the deterministic pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from applyr.config import Config
from applyr.core import analytics
from applyr.core.db import session_scope
from applyr.core.events import days_in_stage, derived_status
from applyr.core.models import Application
from applyr.core.repos import applications as apps_repo
from applyr.core.sla import days_since_activity, is_ghosted, is_stale
from applyr.llm import say
from applyr.llm.provider import ProviderError
from applyr.llm.tools import ToolContext, dispatch

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _application_rows(
    session: Session, config: Config, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    """The `applyr list` table as JSON: derived status, SLA flags, quiet days."""
    apps: list[Application]
    if include_archived:
        apps = list(session.exec(select(Application)).all())
    else:
        apps = apps_repo.non_archived(session)
    rows: list[dict[str, Any]] = []
    for application in apps:
        assert application.id is not None
        status = derived_status(session, application.id)
        rows.append(
            {
                "ref": f"app#{application.id}",
                "label": apps_repo.label(session, application).rsplit(" (", 1)[0],
                "status": status.value if status else None,
                "days_in_stage": days_in_stage(session, application.id),
                "days_quiet": days_since_activity(session, application.id),
                "source": application.source,
                "stale": is_stale(session, application.id, config.sla_days),
                "ghosted": is_ghosted(session, application.id, config.sla_days),
                "archived": application.archived,
            }
        )
    return rows


def create_app(config: Config, engine: Engine) -> Starlette:
    def _ctx(session: Session, utterance: str | None = None) -> ToolContext:
        return ToolContext(
            session=session, engine=engine, config=config, source="web", utterance=utterance
        )

    async def index(_: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html")

    async def api_overview(_: Request) -> Response:
        with session_scope(engine) as session:
            brief = analytics.briefing(session, config.sla_days)
            return JSONResponse(
                {
                    "briefing": brief.model_dump(mode="json"),
                    "funnel": [s.model_dump(mode="json") for s in analytics.funnel(session)],
                }
            )

    async def api_applications(request: Request) -> Response:
        include_archived = request.query_params.get("archived") == "1"
        with session_scope(engine) as session:
            return JSONResponse(
                _application_rows(session, config, include_archived=include_archived)
            )

    async def api_stats(_: Request) -> Response:
        with session_scope(engine) as session:
            return JSONResponse(
                {
                    "funnel": [s.model_dump(mode="json") for s in analytics.funnel(session)],
                    "by_resume": [s.model_dump(mode="json") for s in analytics.by_resume(session)],
                    "by_source": [s.model_dump(mode="json") for s in analytics.by_source(session)],
                    "by_time_to_apply": [
                        s.model_dump(mode="json") for s in analytics.by_time_to_apply(session)
                    ],
                }
            )

    async def api_dispatch(request: Request) -> Response:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"result": "error", "message": "invalid JSON body"}, status_code=400
            )
        name = payload.get("name")
        args = payload.get("args") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return JSONResponse(
                {"result": "error", "message": "body must be {name: str, args: object}"},
                status_code=400,
            )
        with session_scope(engine) as session:
            result = dispatch(
                _ctx(session), name, {k: v for k, v in args.items() if v is not None}
            )
            return JSONResponse(result.model_dump(mode="json", exclude_none=True))

    async def api_say(request: Request) -> Response:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"result": "error", "message": "invalid JSON body"}, status_code=400
            )
        text = str(payload.get("text") or "").strip()
        if not text:
            return JSONResponse(
                {"result": "error", "message": "text is required"}, status_code=400
            )
        with session_scope(engine) as session:
            ctx = _ctx(session, utterance=text)
            try:
                planned = say.plan(ctx, text)
            except ProviderError as exc:
                return JSONResponse({"result": "error", "message": str(exc)}, status_code=502)
            if not planned.tool_calls:
                return JSONResponse({"result": "ok", "reply": planned.text or "(no reply)"})
            call = planned.tool_calls[0]
            result = say.run_call(ctx, call)
            body = result.model_dump(mode="json", exclude_none=True)
            # echo the planned call so the UI can retry with a disambiguated ref
            body["tool"] = call.name
            body["args"] = call.arguments
            return JSONResponse(body)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/overview", api_overview),
            Route("/api/applications", api_applications),
            Route("/api/stats", api_stats),
            Route("/api/dispatch", api_dispatch, methods=["POST"]),
            Route("/api/say", api_say, methods=["POST"]),
        ]
    )
