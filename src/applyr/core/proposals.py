"""Propose -> diff -> confirm -> commit. The only write path into the DB.

Resolution happens before propose(); the resolved action (ids frozen) is stored
in action_json together with the original utterance for audit. confirm()
re-validates referential integrity, applies the action, and appends events —
all in the caller's transaction, so a failure rolls everything back including
the proposal's status flip.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from pydantic import BaseModel
from sqlmodel import Session, col, select

from applyr.core import actions as act
from applyr.core.actions import action_adapter
from applyr.core.clock import utcnow
from applyr.core.diff import render_diff
from applyr.core.enums import EventSource, ProposalStatus, Status
from applyr.core.events import append_note_event, append_status_event
from applyr.core.models import (
    Application,
    Contact,
    Interaction,
    Interview,
    Job,
    Proposal,
    TaskItem,
)
from applyr.core.repos import applications as apps_repo
from applyr.core.repos import companies as companies_repo
from applyr.core.repos import documents as documents_repo


class ProposalError(Exception):
    pass


class CommitError(Exception):
    pass


class CommitResult(BaseModel):
    proposal_id: int
    refs: dict[str, int]
    summary: str


_EVENT_SOURCE: dict[str, EventSource] = {
    "cli": EventSource.MANUAL,
    "email": EventSource.EMAIL,
    "say": EventSource.LLM,
    "mcp": EventSource.LLM,
}


def propose(
    session: Session,
    action: act.Action,
    *,
    source: str = "cli",
    utterance: str | None = None,
) -> Proposal:
    wrapper: dict[str, Any] = {
        "action": action.model_dump(mode="json"),
        "utterance": utterance,
    }
    proposal = Proposal(
        source=source,
        action_json=json.dumps(wrapper),
        status=ProposalStatus.PENDING.value,
    )
    session.add(proposal)
    session.flush()
    return proposal


def load_action(proposal: Proposal) -> act.Action:
    wrapper = json.loads(proposal.action_json)
    return action_adapter.validate_python(wrapper["action"])


def load_utterance(proposal: Proposal) -> str | None:
    raw = json.loads(proposal.action_json).get("utterance")
    return str(raw) if raw is not None else None


def diff_for(session: Session, proposal: Proposal) -> str:
    return render_diff(session, load_action(proposal))


def pending(session: Session) -> list[Proposal]:
    stmt = (
        select(Proposal)
        .where(Proposal.status == ProposalStatus.PENDING.value)
        .order_by(col(Proposal.created_at), col(Proposal.id))
    )
    return list(session.exec(stmt).all())


def reject(session: Session, proposal_id: int, reason: str | None = None) -> Proposal:
    proposal = session.get(Proposal, proposal_id)
    if proposal is None:
        raise ProposalError(f"proposal #{proposal_id} not found")
    if proposal.status != ProposalStatus.PENDING.value:
        raise ProposalError(f"proposal #{proposal_id} is already {proposal.status}")
    if reason:
        wrapper = json.loads(proposal.action_json)
        wrapper["reject_reason"] = reason
        proposal.action_json = json.dumps(wrapper)
    proposal.status = ProposalStatus.REJECTED.value
    proposal.resolved_at = utcnow()
    session.add(proposal)
    session.flush()
    return proposal


def confirm(session: Session, proposal_id: int) -> CommitResult:
    proposal = session.get(Proposal, proposal_id)
    if proposal is None:
        raise ProposalError(f"proposal #{proposal_id} not found")
    if proposal.status != ProposalStatus.PENDING.value:
        raise ProposalError(f"proposal #{proposal_id} is already {proposal.status}")
    action = load_action(proposal)
    source = _EVENT_SOURCE.get(proposal.source, EventSource.MANUAL)
    refs, summary = _apply(session, action, source)
    proposal.status = ProposalStatus.ACCEPTED.value
    proposal.resolved_at = utcnow()
    session.add(proposal)
    session.flush()
    assert proposal.id is not None
    return CommitResult(proposal_id=proposal.id, refs=refs, summary=summary)


def _require_application(session: Session, application_id: int) -> Application:
    app = session.get(Application, application_id)
    if app is None:
        raise CommitError(f"application app#{application_id} no longer exists")
    return app


def _apply(
    session: Session, action: act.Action, source: EventSource
) -> tuple[dict[str, int], str]:
    if isinstance(action, act.AddCompany):
        company = companies_repo.create(session, action.company)
        assert company.id is not None
        return {"company": company.id}, f"created company {company.name} (co#{company.id})"

    if isinstance(action, act.AddJob):
        return _apply_add_job(session, action, source)

    if isinstance(action, act.LogApplication):
        return _apply_log_application(session, action, source)

    if isinstance(action, act.UpdateStatus):
        app = _require_application(session, action.application_id)
        append_status_event(
            session,
            action.application_id,
            action.to_status,
            occurred_at=action.occurred_at,
            source=source,
            note=action.note,
        )
        return (
            {"application": action.application_id},
            f"{apps_repo.label(session, app)}: -> {action.to_status.value}",
        )

    if isinstance(action, act.LogInteraction):
        if action.application_id is not None:
            _require_application(session, action.application_id)
        if action.contact_id is not None and session.get(Contact, action.contact_id) is None:
            raise CommitError(f"contact#{action.contact_id} no longer exists")
        row = Interaction(
            contact_id=action.contact_id,
            application_id=action.application_id,
            channel=action.channel.value,
            direction=action.direction.value,
            occurred_at=action.occurred_at or utcnow(),
            summary=action.summary,
        )
        session.add(row)
        session.flush()
        assert row.id is not None
        return {"interaction": row.id}, f"logged {action.direction.value} interaction"

    if isinstance(action, act.LogInterview):
        _require_application(session, action.application_id)
        if action.round is None:
            existing = session.exec(
                select(Interview).where(Interview.application_id == action.application_id)
            ).all()
            round_no = max((iv.round for iv in existing), default=0) + 1
        else:
            round_no = action.round
        interview_row = Interview(
            application_id=action.application_id,
            round=round_no,
            scheduled_at=action.scheduled_at,
            format=action.format.value,
            interviewers_json=json.dumps(action.interviewers),
        )
        session.add(interview_row)
        session.flush()
        assert interview_row.id is not None
        return (
            {"interview": interview_row.id},
            f"logged interview round {round_no} (int#{interview_row.id})",
        )

    if isinstance(action, act.LogDebrief):
        interview = session.get(Interview, action.interview_id)
        if interview is None:
            raise CommitError(f"interview int#{action.interview_id} no longer exists")
        interview.debrief_notes = action.notes
        interview.questions_asked_json = json.dumps(action.questions_asked)
        interview.outcome = action.outcome.value
        session.add(interview)
        append_note_event(
            session,
            interview.application_id,
            f"Debrief round {interview.round}: {action.outcome.value}",
            source=source,
        )
        session.flush()
        return (
            {"interview": action.interview_id},
            f"debrief saved for round {interview.round}",
        )

    if isinstance(action, act.AddContact):
        company_id = action.company_id
        refs: dict[str, int] = {}
        if action.new_company is not None:
            company = companies_repo.create(session, action.new_company)
            assert company.id is not None
            company_id = company.id
            refs["company"] = company.id
        contact_row = Contact(
            name=action.name,
            company_id=company_id,
            title=action.title,
            email=action.email,
            linkedin=action.linkedin,
            relationship=action.relationship,
            notes=action.notes,
        )
        session.add(contact_row)
        session.flush()
        assert contact_row.id is not None
        refs["contact"] = contact_row.id
        return refs, f"added contact {action.name} (contact#{contact_row.id})"

    if isinstance(action, act.AddNote):
        app = _require_application(session, action.application_id)
        append_note_event(session, action.application_id, action.text, source=source)
        label = apps_repo.label(session, app)
        return {"application": action.application_id}, f"note added to {label}"

    if isinstance(action, act.AddTask):
        if action.application_id is not None:
            _require_application(session, action.application_id)
        task_row = TaskItem(
            application_id=action.application_id,
            due_at=action.due_at,
            kind=action.task_kind.value,
            description=action.description,
        )
        session.add(task_row)
        session.flush()
        assert task_row.id is not None
        return {"task": task_row.id}, f"task added (task#{task_row.id})"

    raise CommitError(f"unknown action kind: {getattr(action, 'kind', '?')}")


def _apply_add_job(
    session: Session, action: act.AddJob, source: EventSource
) -> tuple[dict[str, int], str]:
    refs: dict[str, int] = {}
    company_id = action.company_id
    if action.new_company is not None:
        company = companies_repo.create(session, action.new_company)
        assert company.id is not None
        company_id = company.id
        refs["company"] = company.id
    assert company_id is not None
    job = Job(
        company_id=company_id,
        title=action.title,
        level=action.level,
        location=action.location,
        remote_policy=action.remote_policy,
        comp_min=action.comp_min,
        comp_max=action.comp_max,
        currency=action.currency,
        source=action.source,
        url=action.url,
        posted_at=action.posted_at,
        jd_markdown=action.jd_markdown,
        jd_snapshot_path=action.jd_snapshot_path,
        jd_hash=action.jd_hash,
    )
    session.add(job)
    session.flush()
    assert job.id is not None
    refs["job"] = job.id
    summary = f"added job {action.title} (job#{job.id})"
    if action.save:
        app = Application(job_id=job.id)
        session.add(app)
        session.flush()
        assert app.id is not None
        append_status_event(session, app.id, Status.SAVED, source=source)
        refs["application"] = app.id
        summary += f", saved as app#{app.id}"
    return refs, summary


def _apply_log_application(
    session: Session, action: act.LogApplication, source: EventSource
) -> tuple[dict[str, int], str]:
    job = session.get(Job, action.job_id)
    if job is None:
        raise CommitError(f"job#{action.job_id} no longer exists")
    refs: dict[str, int] = {}

    resume_id = action.resume_document_id
    if action.new_resume is not None:
        doc = documents_repo.register(
            session, action.new_resume.file_path, action.new_resume.type, action.new_resume.label
        )
        assert doc.id is not None
        resume_id = doc.id
        refs["resume_document"] = doc.id
    cover_id = action.cover_letter_document_id
    if action.new_cover_letter is not None:
        doc = documents_repo.register(
            session,
            action.new_cover_letter.file_path,
            action.new_cover_letter.type,
            action.new_cover_letter.label,
        )
        assert doc.id is not None
        cover_id = doc.id
        refs["cover_letter_document"] = doc.id

    # Reuse a saved (not-yet-applied) application for this job if one exists.
    existing = [
        a
        for a in apps_repo.for_job(session, action.job_id)
        if a.applied_at is None and not a.archived
    ]
    app = existing[0] if existing else None
    if app is None:
        app = Application(job_id=action.job_id)
        session.add(app)
        session.flush()
    if resume_id is not None:
        app.resume_document_id = resume_id
    if cover_id is not None:
        app.cover_letter_document_id = cover_id
    if action.source is not None:
        app.source = action.source
    if action.referral_contact_id is not None:
        app.referral_contact_id = action.referral_contact_id
    if action.priority is not None:
        app.priority = action.priority
    session.add(app)
    session.flush()
    assert app.id is not None

    applied_date = action.applied_at or utcnow().date()
    occurred = dt.datetime.combine(applied_date, dt.time(12, 0))
    append_status_event(session, app.id, Status.APPLIED, occurred_at=occurred, source=source)
    refs["application"] = app.id
    return refs, f"applied: {apps_repo.label(session, app)}"
