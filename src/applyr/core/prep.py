"""Interview prep dossier: assembled deterministically from what's in the DB.

Inputs: archived JD, the pinned resume actually sent, company notes, previous
round debriefs (with the questions asked), and interviewer names. An optional
LLM section (likely questions, talking points) is appended by the CLI when a
provider is reachable — the dossier never requires one.
"""

from __future__ import annotations

import json

from sqlmodel import Session, select

from applyr.core.clock import utcnow
from applyr.core.events import derived_status, notes_for
from applyr.core.models import Application, Company, Contact, Document, Interview, Job


class PrepError(Exception):
    pass


def build_dossier(session: Session, application_id: int) -> str:
    app = session.get(Application, application_id)
    if app is None:
        raise PrepError(f"app#{application_id} not found")
    job = session.get(Job, app.job_id)
    company = session.get(Company, job.company_id) if job else None

    lines: list[str] = []
    title = job.title if job else "?"
    company_name = company.name if company else "?"
    lines.append(f"# Prep dossier: {company_name} — {title}")
    status = derived_status(session, application_id)
    lines.append(
        f"\nGenerated {utcnow().date().isoformat()} · status: "
        f"{status.value if status else '?'} · app#{application_id}"
    )

    interviews = sorted(
        session.exec(
            select(Interview).where(Interview.application_id == application_id)
        ).all(),
        key=lambda iv: iv.scheduled_at,
    )
    now = utcnow()
    upcoming = [iv for iv in interviews if iv.scheduled_at >= now]
    if upcoming:
        iv = upcoming[0]
        interviewers = json.loads(iv.interviewers_json)
        lines.append(
            f"\n## Next interview\n\nRound {iv.round}, {iv.format}, "
            f"{iv.scheduled_at.isoformat(sep=' ', timespec='minutes')} UTC"
        )
        if interviewers:
            lines.append(f"Interviewers: {', '.join(interviewers)}")
            for name in interviewers:
                contact = session.exec(
                    select(Contact).where(Contact.name == name)
                ).first()
                if contact and (contact.title or contact.notes):
                    detail = " — ".join(p for p in [contact.title, contact.notes] if p)
                    lines.append(f"  - {name}: {detail}")

    if company is not None:
        company_bits = [
            f"industry: {company.industry}" if company.industry else None,
            f"size: {company.size}" if company.size else None,
            f"HQ: {company.hq}" if company.hq else None,
        ]
        header = " · ".join(b for b in company_bits if b)
        lines.append(f"\n## Company: {company.name}" + (f"\n\n{header}" if header else ""))
        if company.notes:
            lines.append(company.notes)

    notes = notes_for(session, application_id)
    if notes:
        lines.append("\n## Notes on this application\n")
        for occurred_at, text in notes:
            lines.append(f"- {occurred_at.date().isoformat()}: {text}")

    past = [iv for iv in interviews if iv.scheduled_at < now]
    debriefed = [iv for iv in past if iv.debrief_notes or iv.questions_asked_json != "[]"]
    if debriefed:
        lines.append("\n## Previous rounds\n")
        for iv in debriefed:
            when = iv.scheduled_at.date().isoformat()
            lines.append(f"### Round {iv.round} ({when}, {iv.format})")
            if iv.outcome:
                lines.append(f"Outcome: {iv.outcome}")
            questions = json.loads(iv.questions_asked_json)
            if questions:
                lines.append("Questions asked:")
                for q in questions:
                    lines.append(f"- {q}")
            if iv.debrief_notes:
                lines.append(f"Debrief: {iv.debrief_notes}")
            lines.append("")

    resume = (
        session.get(Document, app.resume_document_id) if app.resume_document_id else None
    )
    if resume is not None:
        lines.append(
            f"\n## Resume sent: {resume.label}\n\n"
            f"(file: {resume.file_path}, sha256 {resume.content_hash[:12]})"
        )
        if resume.extracted_text:
            lines.append("\n```\n" + resume.extracted_text.strip()[:4000] + "\n```")
    else:
        lines.append("\n## Resume sent: none pinned")

    if job is not None and job.jd_markdown:
        lines.append(f"\n## Job description (archived {job.captured_at.date().isoformat()})\n")
        lines.append(job.jd_markdown.strip())
    else:
        lines.append("\n## Job description: not captured")

    return "\n".join(lines)


def question_bank(session: Session) -> list[tuple[str, int, str]]:
    """(company_name, round, question) for every debriefed question, newest first."""
    out: list[tuple[str, int, str]] = []
    interviews = session.exec(select(Interview)).all()
    for iv in sorted(interviews, key=lambda i: i.scheduled_at, reverse=True):
        questions = json.loads(iv.questions_asked_json)
        if not questions:
            continue
        app = session.get(Application, iv.application_id)
        job = session.get(Job, app.job_id) if app else None
        company = session.get(Company, job.company_id) if job else None
        name = company.name if company else "?"
        for q in questions:
            out.append((name, iv.round, str(q)))
    return out
