"""Human-readable diffs for proposals. What you see is what confirm() commits."""

from __future__ import annotations

from sqlmodel import Session

from jobtrack.core import actions as act
from jobtrack.core.enums import STATUS_ORDER, TERMINAL_STATUSES
from jobtrack.core.events import days_in_stage, derived_status
from jobtrack.core.models import Application, Company, Contact, Interview
from jobtrack.core.repos import applications as apps_repo


def _company_line(session: Session, company_id: int | None, new: act.NewCompany | None) -> str:
    if new is not None:
        extra = f" ({new.domain})" if new.domain else ""
        return f"+ company: {new.name}{extra}  [NEW]"
    company = session.get(Company, company_id) if company_id is not None else None
    return f"  company: {company.name if company else '?'} (co#{company_id})"


def _app_header(session: Session, application_id: int) -> str:
    app = session.get(Application, application_id)
    if app is None:
        return f"  application: app#{application_id} (missing!)"
    status = derived_status(session, application_id)
    days = days_in_stage(session, application_id)
    status_part = f"{status.value}, {days}d in stage" if status else "no events"
    return f"  {apps_repo.label(session, app)} — currently: {status_part}"


def render_diff(session: Session, action: act.Action) -> str:
    lines: list[str] = []
    if isinstance(action, act.AddCompany):
        lines.append(_company_line(session, None, action.company))
    elif isinstance(action, act.AddJob):
        lines.append(_company_line(session, action.company_id, action.new_company))
        comp = ""
        if action.comp_min or action.comp_max:
            lo, hi = action.comp_min or "?", action.comp_max or "?"
            comp = f"  comp: {lo}-{hi} {action.currency or ''}"
        details = ", ".join(
            p
            for p in [action.level, action.location, action.remote_policy, action.source]
            if p
        )
        lines.append(f"+ job: {action.title}" + (f" ({details})" if details else ""))
        if comp:
            lines.append(comp)
        if action.jd_markdown:
            short_hash = action.jd_hash[:12] if action.jd_hash else "?"
            lines.append(
                f"  JD archived: {len(action.jd_markdown)} chars, hash {short_hash}"
            )
        if action.posted_at:
            lines.append(f"  posted: {action.posted_at.isoformat()}")
        if action.save:
            lines.append("+ application: saved")
    elif isinstance(action, act.LogApplication):
        when = f" on {action.applied_at.isoformat()}" if action.applied_at else " (today)"
        lines.append(f"+ event: -> applied{when}")
        if action.new_resume:
            lines.append(
                f"+ document: {action.new_resume.label} "
                f"({action.new_resume.file_path})  [NEW]"
            )
        if action.resume_document_id:
            lines.append(f"  resume pinned: doc#{action.resume_document_id}")
        if action.new_cover_letter:
            lines.append(f"+ document: {action.new_cover_letter.label}  [NEW]")
        if action.cover_letter_document_id:
            lines.append(f"  cover letter pinned: doc#{action.cover_letter_document_id}")
        if action.source:
            lines.append(f"  source: {action.source}")
        if action.referral_contact_id:
            lines.append(f"  referral: contact#{action.referral_contact_id}")
    elif isinstance(action, act.UpdateStatus):
        lines.append(_app_header(session, action.application_id))
        current = derived_status(session, action.application_id)
        arrow_from = current.value if current else "?"
        when = ""
        if action.occurred_at:
            when = f"  ({action.occurred_at.isoformat(sep=' ', timespec='minutes')} UTC)"
        lines.append(f"~ status: {arrow_from} -> {action.to_status.value}{when}")
        if action.note:
            lines.append(f"  note: {action.note}")
        if current is not None:
            if current in TERMINAL_STATUSES:
                lines.append(f"  ! unusual: leaving terminal status {current.value}")
            elif (
                current in STATUS_ORDER
                and action.to_status in STATUS_ORDER
                and STATUS_ORDER[action.to_status] < STATUS_ORDER[current]
            ):
                lines.append("  ! unusual: moving backwards in the pipeline")
            if current == action.to_status:
                lines.append(f"  ! no-op: already {current.value}")
    elif isinstance(action, act.LogInteraction):
        target = []
        if action.application_id:
            target.append(_app_header(session, action.application_id))
        if action.contact_id:
            contact = session.get(Contact, action.contact_id)
            name = contact.name if contact else "?"
            target.append(f"  contact: {name} (contact#{action.contact_id})")
        lines.extend(target)
        lines.append(
            f"+ interaction: {action.direction.value} via {action.channel.value}: {action.summary}"
        )
    elif isinstance(action, act.LogInterview):
        lines.append(_app_header(session, action.application_id))
        round_part = f"round {action.round}" if action.round else "next round"
        who = f" with {', '.join(action.interviewers)}" if action.interviewers else ""
        lines.append(
            f"+ interview: {round_part}, {action.format.value}, "
            f"{action.scheduled_at.isoformat(sep=' ', timespec='minutes')} UTC{who}"
        )
    elif isinstance(action, act.LogDebrief):
        interview = session.get(Interview, action.interview_id)
        if interview is not None:
            lines.append(_app_header(session, interview.application_id))
            lines.append(f"~ debrief for round {interview.round} (int#{action.interview_id})")
        else:
            lines.append(f"~ debrief for int#{action.interview_id} (missing!)")
        lines.append(f"  outcome: {action.outcome.value}")
        for q in action.questions_asked:
            lines.append(f"  Q: {q}")
        lines.append(f"  notes: {action.notes[:200]}")
    elif isinstance(action, act.AddContact):
        lines.append(_company_line(session, action.company_id, action.new_company))
        details = ", ".join(p for p in [action.title, action.email, action.relationship] if p)
        lines.append(f"+ contact: {action.name}" + (f" ({details})" if details else ""))
    elif isinstance(action, act.AddNote):
        lines.append(_app_header(session, action.application_id))
        lines.append(f"+ note: {action.text}")
    elif isinstance(action, act.AddTask):
        if action.application_id:
            lines.append(_app_header(session, action.application_id))
        lines.append(
            f"+ task: {action.task_kind.value} due "
            f"{action.due_at.isoformat(sep=' ', timespec='minutes')} UTC — {action.description}"
        )
    return "\n".join(lines)
