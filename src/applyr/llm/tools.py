"""Tool registry: the one place tool schemas live.

The MCP server exposes these to Claude; `applyr say` passes the same schemas
to the local model. Handlers resolve refs deterministically, then either create
a pending Proposal (writes), return data (reads), or return a disambiguation
request with candidates — never a guess.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from applyr.config import Config
from applyr.core import actions as act
from applyr.core import analytics
from applyr.core import proposals as props
from applyr.core.actions import is_auto_approvable
from applyr.core.clock import utcnow
from applyr.core.db import vec_available
from applyr.core.enums import (
    Direction,
    DocumentType,
    InteractionChannel,
    InterviewFormat,
    InterviewOutcome,
    ProposalStatus,
    Status,
    TaskKind,
)
from applyr.core.events import days_in_stage, derived_status, last_activity, notes_for
from applyr.core.models import (
    Application,
    ApplicationEvent,
    Company,
    Contact,
    Document,
    Email,
    Interview,
    Job,
    Proposal,
)
from applyr.core.repos import applications as apps_repo
from applyr.core.repos import documents as documents_repo
from applyr.core.search import (
    SemanticUnavailable,
    fuzzy_search,
    semantic_search,
)
from applyr.ingest.jd_capture import CaptureError, capture_text, capture_url
from applyr.llm import resolution as res
from applyr.llm.provider import ProviderError

# --- envelope ---------------------------------------------------------------


class ProposalView(BaseModel):
    id: int
    kind: str
    diff: str
    auto_approvable: bool


class CandidateView(BaseModel):
    ref: str
    label: str
    status: str | None = None
    last_activity: str | None = None


class ToolResult(BaseModel):
    result: Literal["ok", "proposal_created", "needs_disambiguation", "error"]
    data: dict[str, Any] | None = None
    proposal: ProposalView | None = None
    candidates: list[CandidateView] | None = None
    message: str | None = None


@dataclass
class ToolContext:
    session: Session
    engine: Engine
    config: Config
    source: str = "mcp"  # or "say"
    utterance: str | None = None


def _candidates(resolution: res.Resolution) -> list[CandidateView]:
    return [
        CandidateView(
            ref=c.ref, label=c.label, status=c.status, last_activity=c.last_activity
        )
        for c in resolution.candidates
    ]


def _disambiguate(resolution: res.Resolution, param: str) -> ToolResult:
    return ToolResult(
        result="needs_disambiguation",
        candidates=_candidates(resolution),
        data={"disambiguate_param": param},
        message=(resolution.hint or "ambiguous reference")
        + f" — call again with `{param}` set to one of the candidate refs",
    )


def _error(message: str) -> ToolResult:
    return ToolResult(result="error", message=message)


def _propose(ctx: ToolContext, action: act.Action, note: str | None = None) -> ToolResult:
    proposal = props.propose(ctx.session, action, source=ctx.source, utterance=ctx.utterance)
    assert proposal.id is not None
    kind = str(action.kind)
    view = ProposalView(
        id=proposal.id,
        kind=kind,
        diff=props.diff_for(ctx.session, proposal),
        auto_approvable=is_auto_approvable(kind, ctx.config.auto_approve),
    )
    message = "pending — show the diff to the user, then confirm_proposal or reject_proposal"
    if note:
        message = f"{note}. {message}"
    return ToolResult(result="proposal_created", proposal=view, message=message)


# --- parameter models (these define the JSON schemas the LLM sees) ----------


class AddJobParams(BaseModel):
    """Capture a job posting (and by default save it to the pipeline)."""

    company: str = Field(description="Company name or co#id")
    title: str
    jd_text: str | None = Field(default=None, description="Pasted job description text")
    jd_url: str | None = Field(
        default=None, description="URL to fetch the JD from (single plain GET)"
    )
    url: str | None = Field(default=None, description="Posting URL kept for the record")
    source: str | None = Field(
        default=None, description="Where it was found: linkedin|company_site|aggregator|other"
    )
    location: str | None = None
    remote_policy: Literal["onsite", "hybrid", "remote"] | None = None
    level: str | None = None
    comp_min: int | None = None
    comp_max: int | None = None
    currency: str | None = Field(default=None, description="ISO-4217, e.g. EUR")
    posted_at: dt.date | None = None
    save: bool = Field(default=True, description="Also create the application at `saved`")


class LogApplicationParams(BaseModel):
    """Record that an application was submitted for a captured job."""

    job: str = Field(description="Job reference: job#id or free text like 'Stripe backend'")
    applied_at: dt.date | None = Field(default=None, description="Defaults to today")
    resume: str | None = Field(
        default=None, description="Resume document label, doc#id, or a file path to register"
    )
    cover_letter: str | None = None
    source: str | None = Field(
        default=None, description="Channel: referral|direct|easy_apply|recruiter"
    )
    referral_contact: str | None = Field(default=None, description="Contact name or contact#id")
    priority: int | None = Field(default=None, description="1 (high) .. 3 (low)")


class UpdateStatusParams(BaseModel):
    """Move an application to a new pipeline status."""

    application: str = Field(description="app#id or free text like 'Stripe backend role'")
    to_status: Status
    occurred_at: dt.datetime | None = Field(
        default=None, description="When it happened (UTC ISO); defaults to now"
    )
    note: str | None = None


class LogInteractionParams(BaseModel):
    """Log a touchpoint with a company or contact (call, email, LinkedIn...)."""

    summary: str
    application: str | None = None
    contact: str | None = None
    channel: InteractionChannel = InteractionChannel.EMAIL
    direction: Direction = Direction.INBOUND
    occurred_at: dt.datetime | None = None


class LogInterviewParams(BaseModel):
    """Schedule/record an interview round."""

    application: str
    scheduled_at: dt.datetime
    round: int | None = Field(default=None, description="Defaults to previous round + 1")
    format: InterviewFormat = InterviewFormat.VIDEO
    interviewers: list[str] = Field(default_factory=list)


class LogDebriefParams(BaseModel):
    """Store a post-interview debrief, including the questions asked."""

    interview: str = Field(
        description="int#id, or an application reference (picks the latest past round)"
    )
    notes: str
    questions_asked: list[str] = Field(default_factory=list)
    outcome: InterviewOutcome = InterviewOutcome.PENDING


class AddContactParams(BaseModel):
    """Add a recruiter/referrer/interviewer contact."""

    name: str
    company: str | None = None
    title: str | None = None
    email: str | None = None
    linkedin: str | None = None
    relationship: str | None = Field(
        default=None, description="recruiter|referrer|interviewer|peer|other"
    )
    notes: str | None = None


class AddNoteParams(BaseModel):
    """Attach a free-text note to an application."""

    application: str
    text: str


class AddTaskParams(BaseModel):
    """Create a follow-up/prep task."""

    description: str
    due_at: dt.datetime
    application: str | None = None
    kind: TaskKind = TaskKind.FOLLOW_UP


class SearchParams(BaseModel):
    """Search everything (fuzzy + semantic when available)."""

    query: str
    scope: Literal["all", "companies", "jobs", "applications", "contacts", "emails", "notes"] = (
        "all"
    )
    limit: int = 10


class ShowParams(BaseModel):
    """Full detail for one entity (app#/job#/co#/contact#/int#/doc# or free text)."""

    ref: str


class GetBriefingParams(BaseModel):
    """Interviews in 48h, tasks due, stale applications, pending proposals."""


class DraftFollowupParams(BaseModel):
    """Draft a follow-up message. Returns text only — never sends anything."""

    application: str
    kind: Literal["post_application_nudge", "post_interview_thanks", "check_in"] = "check_in"


class ListProposalsParams(BaseModel):
    """List proposals (default: pending) with their diffs."""

    status: Literal["pending", "accepted", "rejected"] = "pending"


class ConfirmProposalParams(BaseModel):
    """Commit a pending proposal AFTER the user has seen the diff and said yes."""

    proposal_id: int


class RejectProposalParams(BaseModel):
    """Reject a pending proposal."""

    proposal_id: int
    reason: str | None = None


# --- handlers ---------------------------------------------------------------


def _handle_add_job(ctx: ToolContext, p: AddJobParams) -> ToolResult:
    company_res = res.resolve_company(ctx.session, p.company)
    company_id: int | None = None
    new_company: act.NewCompany | None = None
    note: str | None = None
    if company_res.outcome == "resolved":
        company_id = company_res.entity_id
    elif company_res.outcome == "ambiguous":
        return _disambiguate(company_res, "company")
    else:
        new_company = act.NewCompany(name=p.company.strip())
        note = f"company {p.company.strip()!r} is new and will be created"

    jd_markdown = jd_hash = snapshot_path = None
    if p.jd_text:
        captured = capture_text(p.jd_text, ctx.config.snapshots_dir)
        jd_markdown, jd_hash, snapshot_path = (
            captured.markdown,
            captured.jd_hash,
            captured.snapshot_path,
        )
    elif p.jd_url:
        try:
            captured = capture_url(p.jd_url, ctx.config.snapshots_dir)
        except CaptureError as exc:
            return _error(str(exc))
        jd_markdown, jd_hash, snapshot_path = (
            captured.markdown,
            captured.jd_hash,
            captured.snapshot_path,
        )

    action = act.AddJob(
        company_id=company_id,
        new_company=new_company,
        title=p.title,
        level=p.level,
        location=p.location,
        remote_policy=p.remote_policy,
        comp_min=p.comp_min,
        comp_max=p.comp_max,
        currency=p.currency,
        source=p.source,
        url=p.url or p.jd_url,
        posted_at=p.posted_at,
        jd_markdown=jd_markdown,
        jd_snapshot_path=snapshot_path,
        jd_hash=jd_hash,
        save=p.save,
    )
    return _propose(ctx, action, note)


def _resolve_document_param(
    ctx: ToolContext, text: str, doc_type: DocumentType
) -> tuple[int | None, act.NewDocument | None, ToolResult | None]:
    doc_res = res.resolve_document(ctx.session, text)
    if doc_res.outcome == "resolved":
        return doc_res.entity_id, None, None
    if doc_res.outcome == "ambiguous":
        return None, None, _disambiguate(doc_res, "resume")
    path = Path(text).expanduser()
    if path.is_file():
        return None, act.NewDocument(file_path=str(path), type=doc_type, label=path.stem), None
    return (
        None,
        None,
        _error(
            f"no document labelled {text!r} and no file at that path — "
            "register one with add_document/`applyr add doc` or pass a valid path"
        ),
    )


def _handle_log_application(ctx: ToolContext, p: LogApplicationParams) -> ToolResult:
    job_res = res.resolve_job(ctx.session, p.job)
    if job_res.outcome == "ambiguous":
        return _disambiguate(job_res, "job")
    if job_res.outcome == "not_found":
        return _error(
            (job_res.hint or "job not found") + " — capture it first with add_job"
        )
    assert job_res.entity_id is not None

    resume_id = new_resume = None
    if p.resume:
        resume_id, new_resume, err = _resolve_document_param(ctx, p.resume, DocumentType.RESUME)
        if err is not None:
            return err
    cover_id = new_cover = None
    if p.cover_letter:
        cover_id, new_cover, err = _resolve_document_param(
            ctx, p.cover_letter, DocumentType.COVER_LETTER
        )
        if err is not None:
            return err

    referral_id: int | None = None
    if p.referral_contact:
        contact_res = res.resolve_contact(ctx.session, p.referral_contact)
        if contact_res.outcome == "ambiguous":
            return _disambiguate(contact_res, "referral_contact")
        if contact_res.outcome == "not_found":
            return _error(
                (contact_res.hint or "contact not found") + " — add them with add_contact first"
            )
        referral_id = contact_res.entity_id

    action = act.LogApplication(
        job_id=job_res.entity_id,
        applied_at=p.applied_at,
        resume_document_id=resume_id,
        new_resume=new_resume,
        cover_letter_document_id=cover_id,
        new_cover_letter=new_cover,
        source=p.source,
        referral_contact_id=referral_id,
        priority=p.priority,
    )
    return _propose(ctx, action, job_res.note)


def _handle_update_status(ctx: ToolContext, p: UpdateStatusParams) -> ToolResult:
    app_res = res.resolve_application(ctx.session, p.application)
    if app_res.outcome == "ambiguous":
        return _disambiguate(app_res, "application")
    if app_res.outcome == "not_found":
        return _error(app_res.hint or "application not found")
    assert app_res.entity_id is not None
    action = act.UpdateStatus(
        application_id=app_res.entity_id,
        to_status=p.to_status,
        occurred_at=p.occurred_at,
        note=p.note,
    )
    return _propose(ctx, action, app_res.note)


def _handle_log_interaction(ctx: ToolContext, p: LogInteractionParams) -> ToolResult:
    application_id = contact_id = None
    note = None
    if p.application:
        app_res = res.resolve_application(ctx.session, p.application)
        if app_res.outcome == "ambiguous":
            return _disambiguate(app_res, "application")
        if app_res.outcome == "not_found":
            return _error(app_res.hint or "application not found")
        application_id = app_res.entity_id
        note = app_res.note
    if p.contact:
        contact_res = res.resolve_contact(ctx.session, p.contact)
        if contact_res.outcome == "ambiguous":
            return _disambiguate(contact_res, "contact")
        if contact_res.outcome == "not_found":
            return _error(contact_res.hint or "contact not found")
        contact_id = contact_res.entity_id
    if application_id is None and contact_id is None:
        return _error("log_interaction needs an application or a contact")
    action = act.LogInteraction(
        application_id=application_id,
        contact_id=contact_id,
        channel=p.channel,
        direction=p.direction,
        summary=p.summary,
        occurred_at=p.occurred_at,
    )
    return _propose(ctx, action, note)


def _handle_log_interview(ctx: ToolContext, p: LogInterviewParams) -> ToolResult:
    app_res = res.resolve_application(ctx.session, p.application)
    if app_res.outcome == "ambiguous":
        return _disambiguate(app_res, "application")
    if app_res.outcome == "not_found":
        return _error(app_res.hint or "application not found")
    assert app_res.entity_id is not None
    action = act.LogInterview(
        application_id=app_res.entity_id,
        scheduled_at=p.scheduled_at,
        round=p.round,
        format=p.format,
        interviewers=p.interviewers,
    )
    return _propose(ctx, action, app_res.note)


def _handle_log_debrief(ctx: ToolContext, p: LogDebriefParams) -> ToolResult:
    iv_res = res.resolve_interview(ctx.session, p.interview)
    if iv_res.outcome == "ambiguous":
        return _disambiguate(iv_res, "interview")
    if iv_res.outcome == "not_found":
        return _error(iv_res.hint or "interview not found")
    assert iv_res.entity_id is not None
    action = act.LogDebrief(
        interview_id=iv_res.entity_id,
        notes=p.notes,
        questions_asked=p.questions_asked,
        outcome=p.outcome,
    )
    return _propose(ctx, action, iv_res.note)


def _handle_add_contact(ctx: ToolContext, p: AddContactParams) -> ToolResult:
    company_id: int | None = None
    new_company: act.NewCompany | None = None
    note = None
    if p.company:
        company_res = res.resolve_company(ctx.session, p.company)
        if company_res.outcome == "resolved":
            company_id = company_res.entity_id
        elif company_res.outcome == "ambiguous":
            return _disambiguate(company_res, "company")
        else:
            new_company = act.NewCompany(name=p.company.strip())
            note = f"company {p.company.strip()!r} is new and will be created"
    action = act.AddContact(
        name=p.name,
        company_id=company_id,
        new_company=new_company,
        title=p.title,
        email=p.email,
        linkedin=p.linkedin,
        relationship=p.relationship,
        notes=p.notes,
    )
    return _propose(ctx, action, note)


def _handle_add_note(ctx: ToolContext, p: AddNoteParams) -> ToolResult:
    app_res = res.resolve_application(ctx.session, p.application)
    if app_res.outcome == "ambiguous":
        return _disambiguate(app_res, "application")
    if app_res.outcome == "not_found":
        return _error(app_res.hint or "application not found")
    assert app_res.entity_id is not None
    return _propose(
        ctx, act.AddNote(application_id=app_res.entity_id, text=p.text), app_res.note
    )


def _handle_add_task(ctx: ToolContext, p: AddTaskParams) -> ToolResult:
    application_id = None
    note = None
    if p.application:
        app_res = res.resolve_application(ctx.session, p.application)
        if app_res.outcome == "ambiguous":
            return _disambiguate(app_res, "application")
        if app_res.outcome == "not_found":
            return _error(app_res.hint or "application not found")
        application_id = app_res.entity_id
        note = app_res.note
    action = act.AddTask(
        application_id=application_id,
        due_at=p.due_at,
        task_kind=p.kind,
        description=p.description,
    )
    return _propose(ctx, action, note)


def _handle_search(ctx: ToolContext, p: SearchParams) -> ToolResult:
    hits = fuzzy_search(ctx.session, p.query, scope=p.scope, limit=p.limit)
    semantic_note: str | None = None
    if vec_available(ctx.engine):
        try:
            from applyr.llm.router import local_provider

            provider = local_provider(ctx.config)
            sem = semantic_search(
                ctx.engine,
                ctx.session,
                provider.embed,
                p.query,
                expected_model=ctx.config.llm.embed_model,
                limit=p.limit,
            )
            seen = {h.ref for h in hits}
            hits.extend(h for h in sem if h.ref not in seen)
        except (SemanticUnavailable, ProviderError) as exc:
            semantic_note = f"semantic search skipped: {exc}"
    else:
        semantic_note = "semantic search skipped: sqlite-vec not loaded"
    hits.sort(key=lambda h: h.score, reverse=True)
    return ToolResult(
        result="ok",
        data={"hits": [h.model_dump() for h in hits[: p.limit]]},
        message=semantic_note,
    )


def _application_detail(ctx: ToolContext, application_id: int) -> dict[str, Any]:
    session = ctx.session
    app = session.get(Application, application_id)
    assert app is not None
    job = session.get(Job, app.job_id)
    company = session.get(Company, job.company_id) if job else None
    status = derived_status(session, application_id)
    resume = session.get(Document, app.resume_document_id) if app.resume_document_id else None
    events = [
        {
            "type": ev.type,
            "from": ev.from_status,
            "to": ev.to_status,
            "occurred_at": ev.occurred_at.isoformat(sep=" ", timespec="minutes"),
            "source": ev.source,
            "note": (json.loads(ev.payload_json).get("note") if ev.payload_json else None),
        }
        for ev in sorted(
            session.exec(
                select(ApplicationEvent).where(
                    ApplicationEvent.application_id == application_id
                )
            ).all(),
            key=lambda e: (e.occurred_at, e.id or 0),
        )
    ]
    interviews = [
        {
            "ref": f"int#{iv.id}",
            "round": iv.round,
            "scheduled_at": iv.scheduled_at.isoformat(sep=" ", timespec="minutes"),
            "format": iv.format,
            "interviewers": json.loads(iv.interviewers_json),
            "outcome": iv.outcome,
        }
        for iv in session.exec(
            select(Interview).where(Interview.application_id == application_id)
        ).all()
    ]
    emails = session.exec(
        select(Email).where(Email.linked_application_id == application_id)
    ).all()
    activity = last_activity(session, application_id)
    return {
        "ref": f"app#{application_id}",
        "company": company.name if company else None,
        "title": job.title if job else None,
        "job_ref": f"job#{app.job_id}",
        "status": status.value if status else None,
        "days_in_stage": days_in_stage(session, application_id),
        "applied_at": app.applied_at.isoformat() if app.applied_at else None,
        "source": app.source,
        "priority": app.priority,
        "archived": app.archived,
        "resume": resume.label if resume else None,
        "jd_archived": bool(job.jd_markdown) if job else False,
        "url": job.url if job else None,
        "last_activity": activity.isoformat(sep=" ", timespec="minutes") if activity else None,
        "events": events,
        "interviews": interviews,
        "notes": [
            {"at": ts.date().isoformat(), "text": text}
            for ts, text in notes_for(session, application_id)
        ],
        "linked_emails": len(emails),
    }


def _handle_show(ctx: ToolContext, p: ShowParams) -> ToolResult:
    parsed = res.parse_ref(p.ref)
    if parsed is None:
        app_res = res.resolve_application(ctx.session, p.ref)
        if app_res.outcome == "ambiguous":
            return _disambiguate(app_res, "ref")
        if app_res.outcome == "not_found":
            return _error(app_res.hint or "nothing matched — try search")
        assert app_res.entity_id is not None
        return ToolResult(result="ok", data=_application_detail(ctx, app_res.entity_id))
    kind, entity_id = parsed
    session = ctx.session
    if kind == "app":
        if session.get(Application, entity_id) is None:
            return _error(f"app#{entity_id} not found")
        return ToolResult(result="ok", data=_application_detail(ctx, entity_id))
    if kind == "job":
        job = session.get(Job, entity_id)
        if job is None:
            return _error(f"job#{entity_id} not found")
        company = session.get(Company, job.company_id)
        data = job.model_dump(mode="json")
        data["company"] = company.name if company else None
        data["jd_markdown"] = (job.jd_markdown or "")[:3000]
        return ToolResult(result="ok", data=data)
    if kind == "co":
        company = session.get(Company, entity_id)
        if company is None:
            return _error(f"co#{entity_id} not found")
        data = company.model_dump(mode="json")
        data["applications"] = [
            apps_repo.label(session, a)
            for a in apps_repo.for_company(session, entity_id, include_archived=True)
        ]
        return ToolResult(result="ok", data=data)
    if kind == "contact":
        contact = session.get(Contact, entity_id)
        if contact is None:
            return _error(f"contact#{entity_id} not found")
        return ToolResult(result="ok", data=contact.model_dump(mode="json"))
    if kind == "int":
        interview = session.get(Interview, entity_id)
        if interview is None:
            return _error(f"int#{entity_id} not found")
        data = interview.model_dump(mode="json")
        data["interviewers"] = json.loads(interview.interviewers_json)
        data["questions_asked"] = json.loads(interview.questions_asked_json)
        return ToolResult(result="ok", data=data)
    if kind == "doc":
        doc = session.get(Document, entity_id)
        if doc is None:
            return _error(f"doc#{entity_id} not found")
        data = doc.model_dump(mode="json", exclude={"extracted_text"})
        return ToolResult(result="ok", data=data)
    return _error(f"unsupported ref kind: {kind}")


def _handle_get_briefing(ctx: ToolContext, _p: GetBriefingParams) -> ToolResult:
    brief = analytics.briefing(ctx.session, ctx.config.sla_days)
    return ToolResult(result="ok", data=brief.model_dump(mode="json"))


_FOLLOWUP_TEMPLATES = {
    "post_application_nudge": (
        "Subject: Following up on my {title} application\n\n"
        "Hi{contact_part},\n\nI applied for the {title} role at {company} on {applied_at} "
        "and wanted to check in on where things stand. I remain very interested — happy to "
        "provide anything else that would help.\n\nBest regards"
    ),
    "post_interview_thanks": (
        "Subject: Thank you\n\n"
        "Hi{contact_part},\n\nThank you for taking the time to speak with me about the "
        "{title} role at {company}. I enjoyed the conversation and am excited about the "
        "opportunity. Looking forward to the next steps.\n\nBest regards"
    ),
    "check_in": (
        "Subject: Checking in — {title}\n\n"
        "Hi{contact_part},\n\nI wanted to check in on the {title} process at {company}; "
        "it has been {days_quiet} days since I last heard anything. I'm still very "
        "interested and happy to provide anything you need.\n\nBest regards"
    ),
}


def _handle_draft_followup(ctx: ToolContext, p: DraftFollowupParams) -> ToolResult:
    app_res = res.resolve_application(ctx.session, p.application)
    if app_res.outcome == "ambiguous":
        return _disambiguate(app_res, "application")
    if app_res.outcome == "not_found":
        return _error(app_res.hint or "application not found")
    assert app_res.entity_id is not None
    detail = _application_detail(ctx, app_res.entity_id)
    activity = last_activity(ctx.session, app_res.entity_id)
    days_quiet: int | str = max((utcnow() - activity).days, 0) if activity else "several"
    context = {
        "title": detail["title"] or "the role",
        "company": detail["company"] or "your company",
        "applied_at": detail["applied_at"] or "recently",
        "days_quiet": days_quiet,
        "contact_part": "",
    }
    draft = _FOLLOWUP_TEMPLATES[p.kind].format(**context)
    context_json = json.dumps({k: str(v) for k, v in context.items()})
    try:
        from applyr.llm.provider import ChatMessage
        from applyr.llm.router import provider_for

        provider = provider_for(ctx.config, "draft")
        polished = provider.chat(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "Rewrite the follow-up email draft to be warm, specific and short "
                        "(under 120 words). Keep it honest — no invented details. "
                        "Return only the email text."
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=f"Context: {context_json}\n\nDraft:\n{draft}",
                ),
            ]
        )
        if polished.strip():
            draft = polished.strip()
    except ProviderError:
        pass  # deterministic template is the fallback
    return ToolResult(
        result="ok",
        data={"draft": draft, "application": detail["ref"], "kind": p.kind},
        message="draft only — applyr never sends email",
    )


def _proposal_view(ctx: ToolContext, proposal: Proposal) -> dict[str, Any]:
    action = props.load_action(proposal)
    return {
        "id": proposal.id,
        "kind": str(action.kind),
        "status": proposal.status,
        "source": proposal.source,
        "created_at": proposal.created_at.isoformat(sep=" ", timespec="minutes"),
        "utterance": props.load_utterance(proposal),
        "diff": props.diff_for(ctx.session, proposal)
        if proposal.status == ProposalStatus.PENDING.value
        else None,
    }


def _handle_list_proposals(ctx: ToolContext, p: ListProposalsParams) -> ToolResult:
    stmt = select(Proposal).where(Proposal.status == p.status)
    rows = list(ctx.session.exec(stmt).all())
    return ToolResult(
        result="ok",
        data={"proposals": [_proposal_view(ctx, row) for row in rows]},
    )


def _handle_confirm_proposal(ctx: ToolContext, p: ConfirmProposalParams) -> ToolResult:
    from applyr.core.proposals import CommitError, ProposalError

    try:
        result = props.confirm(ctx.session, p.proposal_id)
    except (ProposalError, CommitError) as exc:
        ctx.session.rollback()  # discard any partial apply before reporting
        return _error(str(exc))
    return ToolResult(
        result="ok",
        data={"refs": result.refs},
        message=result.summary,
    )


def _handle_reject_proposal(ctx: ToolContext, p: RejectProposalParams) -> ToolResult:
    from applyr.core.proposals import ProposalError

    try:
        props.reject(ctx.session, p.proposal_id, p.reason)
    except ProposalError as exc:
        return _error(str(exc))
    return ToolResult(result="ok", message=f"proposal #{p.proposal_id} rejected")


# --- registry ---------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    params_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], ToolResult]
    is_write: bool


def _spec(
    name: str,
    description: str,
    params_model: type[BaseModel],
    handler: Callable[[ToolContext, Any], ToolResult],
    *,
    is_write: bool,
) -> ToolSpec:
    return ToolSpec(name, description, params_model, handler, is_write)


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        _spec(
            "add_job",
            "Capture a job posting with its JD (immutably archived). Creates a pending "
            "proposal; unknown companies are created as part of it.",
            AddJobParams,
            _handle_add_job,
            is_write=True,
        ),
        _spec(
            "log_application",
            "Record that an application was submitted, pinning the exact resume version sent.",
            LogApplicationParams,
            _handle_log_application,
            is_write=True,
        ),
        _spec(
            "update_status",
            "Move an application through the pipeline (applied, screening, interviewing, "
            "offer, rejected...). Always requires confirmation.",
            UpdateStatusParams,
            _handle_update_status,
            is_write=True,
        ),
        _spec(
            "log_interaction",
            "Log a touchpoint with a company or contact.",
            LogInteractionParams,
            _handle_log_interaction,
            is_write=True,
        ),
        _spec(
            "log_interview",
            "Record an interview round (scheduled or completed).",
            LogInterviewParams,
            _handle_log_interview,
            is_write=True,
        ),
        _spec(
            "log_debrief",
            "Store a post-interview debrief and the questions that were asked.",
            LogDebriefParams,
            _handle_log_debrief,
            is_write=True,
        ),
        _spec(
            "add_contact",
            "Add a contact (recruiter, referrer, interviewer).",
            AddContactParams,
            _handle_add_contact,
            is_write=True,
        ),
        _spec(
            "add_note",
            "Attach a note to an application (auto-approvable).",
            AddNoteParams,
            _handle_add_note,
            is_write=True,
        ),
        _spec(
            "add_task",
            "Create a follow-up or prep task with a due date.",
            AddTaskParams,
            _handle_add_task,
            is_write=True,
        ),
        _spec(
            "search",
            "Search companies, jobs, applications, contacts, emails and notes.",
            SearchParams,
            _handle_search,
            is_write=False,
        ),
        _spec(
            "show",
            "Full detail for one entity: events timeline, interviews, pinned docs, notes.",
            ShowParams,
            _handle_show,
            is_write=False,
        ),
        _spec(
            "get_briefing",
            "The daily brief: interviews in 48h, tasks due, stale applications, "
            "pending proposals.",
            GetBriefingParams,
            _handle_get_briefing,
            is_write=False,
        ),
        _spec(
            "draft_followup",
            "Draft (never send) a follow-up message for an application.",
            DraftFollowupParams,
            _handle_draft_followup,
            is_write=False,
        ),
        _spec(
            "list_proposals",
            "List proposals and their diffs.",
            ListProposalsParams,
            _handle_list_proposals,
            is_write=False,
        ),
        _spec(
            "confirm_proposal",
            "Commit a pending proposal after the user approved its diff.",
            ConfirmProposalParams,
            _handle_confirm_proposal,
            is_write=True,
        ),
        _spec(
            "reject_proposal",
            "Reject a pending proposal.",
            RejectProposalParams,
            _handle_reject_proposal,
            is_write=True,
        ),
    ]
}


def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI/Ollama-style function schemas, also consumed by the MCP server."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.params_model.model_json_schema(),
            },
        }
        for spec in TOOLS.values()
    ]


def dispatch(ctx: ToolContext, name: str, raw_args: dict[str, Any]) -> ToolResult:
    spec = TOOLS.get(name)
    if spec is None:
        return _error(f"unknown tool: {name}")
    try:
        params = spec.params_model.model_validate(raw_args)
    except ValidationError as exc:
        return _error(f"invalid arguments for {name}: {exc.errors()}")
    try:
        return spec.handler(ctx, params)
    except (CaptureError, documents_repo.DocumentError) as exc:
        return _error(str(exc))
