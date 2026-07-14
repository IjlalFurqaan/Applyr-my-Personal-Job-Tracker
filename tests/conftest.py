from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

import jobtrack.core.models  # noqa: F401  (populate metadata)
from jobtrack.config import Config
from jobtrack.core.clock import utcnow
from jobtrack.core.enums import EventSource, Status
from jobtrack.core.events import append_status_event
from jobtrack.core.models import Application, Company, Job


@pytest.fixture()
def engine(tmp_path: Path) -> Engine:
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as s:
        yield s


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return Config(home=tmp_path / "home")


def make_company(session: Session, name: str, domain: str | None = None) -> Company:
    company = Company(name=name, domain=domain)
    session.add(company)
    session.flush()
    return company


def make_job(session: Session, company: Company, title: str, **kw: object) -> Job:
    assert company.id is not None
    job = Job(company_id=company.id, title=title, **kw)  # type: ignore[arg-type]
    session.add(job)
    session.flush()
    return job


def make_app(session: Session, job: Job, **kw: object) -> Application:
    assert job.id is not None
    app = Application(job_id=job.id, **kw)  # type: ignore[arg-type]
    session.add(app)
    session.flush()
    return app


def add_status(
    session: Session,
    app: Application,
    status: Status,
    *,
    days_ago: float = 0,
    source: EventSource = EventSource.MANUAL,
) -> None:
    assert app.id is not None
    occurred = utcnow() - dt.timedelta(days=days_ago)
    append_status_event(session, app.id, status, occurred_at=occurred, source=source)


def pipeline(
    session: Session,
    *,
    company: str = "Stripe",
    domain: str | None = None,
    title: str = "Backend Engineer",
    statuses: list[tuple[Status, float]] | None = None,
) -> Application:
    """Company + job + application with a (status, days_ago) event chain."""
    co = make_company(session, company, domain)
    job = make_job(session, co, title)
    app = make_app(session, job)
    for status, days_ago in statuses or []:
        add_status(session, app, status, days_ago=days_ago)
    return app
