from __future__ import annotations

from sqlmodel import Session, col, select

from jobtrack.core.models import Application, Company, Job


def get(session: Session, application_id: int) -> Application | None:
    return session.get(Application, application_id)


def non_archived(session: Session) -> list[Application]:
    stmt = select(Application).where(Application.archived == False)  # noqa: E712
    return list(session.exec(stmt).all())


def for_company(
    session: Session, company_id: int, *, include_archived: bool = False
) -> list[Application]:
    stmt = (
        select(Application)
        .join(Job, col(Application.job_id) == col(Job.id))
        .where(Job.company_id == company_id)
    )
    if not include_archived:
        stmt = stmt.where(Application.archived == False)  # noqa: E712
    return list(session.exec(stmt).all())


def label(session: Session, application: Application) -> str:
    """'Stripe — Backend Engineer (app#3)' for diffs, candidates, and listings."""
    job = session.get(Job, application.job_id)
    company = session.get(Company, job.company_id) if job else None
    company_name = company.name if company else "?"
    title = job.title if job else "?"
    return f"{company_name} — {title} (app#{application.id})"


def for_job(session: Session, job_id: int) -> list[Application]:
    stmt = select(Application).where(Application.job_id == job_id)
    return list(session.exec(stmt).all())
