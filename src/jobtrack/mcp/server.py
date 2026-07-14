"""FastMCP server exposing the tool registry to Claude Code / Claude Desktop.

Wrappers are thin and explicit: each mirrors its params model in
jobtrack.llm.tools (a contract test keeps them in sync) and funnels into
dispatch(). Confirmation over MCP is conversational: write tools return a
pending proposal + diff; the client shows it and then calls confirm_proposal.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from jobtrack.config import Config, ensure_home, load_config
from jobtrack.core.db import init_db, session_scope
from jobtrack.llm.tools import ToolContext, dispatch

mcp: FastMCP = FastMCP(
    "jobtrack",
    instructions=(
        "Personal job-application tracker. Writes create pending proposals — "
        "always show the returned diff to the user and only call confirm_proposal "
        "after they approve. If a result is needs_disambiguation, show the "
        "candidates and retry with the chosen ref."
    ),
)

_state: dict[str, Any] = {}


def _run(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if "engine" not in _state:
        config: Config = load_config()
        ensure_home(config)
        _state["config"] = config
        _state["engine"] = init_db(config.db_path)
    with session_scope(_state["engine"]) as session:
        ctx = ToolContext(
            session=session,
            engine=_state["engine"],
            config=_state["config"],
            source="mcp",
        )
        result = dispatch(ctx, name, {k: v for k, v in args.items() if v is not None})
        return result.model_dump(mode="json", exclude_none=True)


@mcp.tool()
def add_job(
    company: str,
    title: str,
    jd_text: str | None = None,
    jd_url: str | None = None,
    url: str | None = None,
    source: str | None = None,
    location: str | None = None,
    remote_policy: str | None = None,
    level: str | None = None,
    comp_min: int | None = None,
    comp_max: int | None = None,
    currency: str | None = None,
    posted_at: str | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """Capture a job posting (JD archived immutably). Creates a pending proposal."""
    return _run("add_job", dict(locals()))


@mcp.tool()
def log_application(
    job: str,
    applied_at: str | None = None,
    resume: str | None = None,
    cover_letter: str | None = None,
    source: str | None = None,
    referral_contact: str | None = None,
    priority: int | None = None,
) -> dict[str, Any]:
    """Record a submitted application, pinning the exact resume version sent."""
    return _run("log_application", dict(locals()))


@mcp.tool()
def update_status(
    application: str,
    to_status: str,
    occurred_at: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Move an application to a new status (saved/applied/screening/.../rejected)."""
    return _run("update_status", dict(locals()))


@mcp.tool()
def log_interaction(
    summary: str,
    application: str | None = None,
    contact: str | None = None,
    channel: str = "email",
    direction: str = "inbound",
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Log a touchpoint with a company or contact."""
    return _run("log_interaction", dict(locals()))


@mcp.tool()
def log_interview(
    application: str,
    scheduled_at: str,
    round: int | None = None,
    format: str = "video",
    interviewers: list[str] | None = None,
) -> dict[str, Any]:
    """Record an interview round (scheduled or completed)."""
    args = dict(locals())
    if args.get("interviewers") is None:
        args["interviewers"] = []
    return _run("log_interview", args)


@mcp.tool()
def log_debrief(
    interview: str,
    notes: str,
    questions_asked: list[str] | None = None,
    outcome: str = "pending",
) -> dict[str, Any]:
    """Store a post-interview debrief and the questions asked."""
    args = dict(locals())
    if args.get("questions_asked") is None:
        args["questions_asked"] = []
    return _run("log_debrief", args)


@mcp.tool()
def add_contact(
    name: str,
    company: str | None = None,
    title: str | None = None,
    email: str | None = None,
    linkedin: str | None = None,
    relationship: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Add a recruiter/referrer/interviewer contact."""
    return _run("add_contact", dict(locals()))


@mcp.tool()
def add_note(application: str, text: str) -> dict[str, Any]:
    """Attach a free-text note to an application (auto-approvable)."""
    return _run("add_note", dict(locals()))


@mcp.tool()
def add_task(
    description: str,
    due_at: str,
    application: str | None = None,
    kind: str = "follow_up",
) -> dict[str, Any]:
    """Create a follow-up or prep task with a due date."""
    return _run("add_task", dict(locals()))


@mcp.tool()
def search(query: str, scope: str = "all", limit: int = 10) -> dict[str, Any]:
    """Search companies, jobs, applications, contacts, emails and notes."""
    return _run("search", dict(locals()))


@mcp.tool()
def show(ref: str) -> dict[str, Any]:
    """Full detail for one entity (app#12, job#3, co#1, contact#2, int#4, doc#1)."""
    return _run("show", dict(locals()))


@mcp.tool()
def get_briefing() -> dict[str, Any]:
    """Interviews in 48h, tasks due, stale applications, pending proposals."""
    return _run("get_briefing", {})


@mcp.tool()
def draft_followup(application: str, kind: str = "check_in") -> dict[str, Any]:
    """Draft (never send) a follow-up: post_application_nudge | post_interview_thanks | check_in."""
    return _run("draft_followup", dict(locals()))


@mcp.tool()
def list_proposals(status: str = "pending") -> dict[str, Any]:
    """List proposals and their diffs (pending | accepted | rejected)."""
    return _run("list_proposals", dict(locals()))


@mcp.tool()
def confirm_proposal(proposal_id: int) -> dict[str, Any]:
    """Commit a pending proposal — only after the user has approved its diff."""
    return _run("confirm_proposal", dict(locals()))


@mcp.tool()
def reject_proposal(proposal_id: int, reason: str | None = None) -> dict[str, Any]:
    """Reject a pending proposal."""
    return _run("reject_proposal", dict(locals()))


def serve() -> None:
    mcp.run()
