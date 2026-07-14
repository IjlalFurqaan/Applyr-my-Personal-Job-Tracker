from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from applyr.core import analytics
from applyr.core.clock import utcnow
from applyr.core.enums import Status
from applyr.core.models import Document, Interaction, Interview, JobSkill, TaskItem
from tests.conftest import add_status, make_app, make_company, make_job, pipeline


def test_funnel_counts_and_conversion(session: Session) -> None:
    # 3 applied; 2 reach screening; 1 reaches interviewing.
    pipeline(session, company="A", statuses=[(Status.APPLIED, 20)])
    pipeline(session, company="B", statuses=[(Status.APPLIED, 20), (Status.SCREENING, 15)])
    pipeline(
        session,
        company="C",
        statuses=[(Status.APPLIED, 20), (Status.SCREENING, 10), (Status.INTERVIEWING, 5)],
    )
    stages = {s.stage: s for s in analytics.funnel(session)}
    assert stages["applied"].reached == 3
    assert stages["applied"].progressed == 2
    assert stages["applied"].conversion == 2 / 3
    assert stages["screening"].reached == 2
    assert stages["screening"].progressed == 1


def test_funnel_median_days(session: Session) -> None:
    pipeline(session, company="A", statuses=[(Status.APPLIED, 10), (Status.SCREENING, 6)])
    pipeline(session, company="B", statuses=[(Status.APPLIED, 20), (Status.SCREENING, 10)])
    stages = {s.stage: s for s in analytics.funnel(session)}
    # deltas are 4 and 10 days -> median 7
    assert stages["applied"].median_days_to_next == 7.0


def test_response_kind(session: Session) -> None:
    silent = pipeline(session, company="A", statuses=[(Status.APPLIED, 10)])
    rejected = pipeline(
        session, company="B", statuses=[(Status.APPLIED, 10), (Status.REJECTED, 5)]
    )
    progressed = pipeline(
        session, company="C", statuses=[(Status.APPLIED, 10), (Status.SCREENING, 5)]
    )
    pinged = pipeline(session, company="D", statuses=[(Status.APPLIED, 10)])
    assert pinged.id is not None
    session.add(
        Interaction(
            application_id=pinged.id, direction="inbound", summary="recruiter reply"
        )
    )
    session.flush()
    assert silent.id and rejected.id and progressed.id
    assert analytics.response_kind(session, silent.id) == "none"
    assert analytics.response_kind(session, rejected.id) == "response"
    assert analytics.response_kind(session, progressed.id) == "positive"
    assert analytics.response_kind(session, pinged.id) == "response"


def test_by_resume_attribution(session: Session) -> None:
    doc_a = Document(type="resume", label="v1", file_path="x", content_hash="a")
    doc_b = Document(type="resume", label="v2", file_path="y", content_hash="b")
    session.add(doc_a)
    session.add(doc_b)
    session.flush()
    app1 = pipeline(session, company="A", statuses=[(Status.APPLIED, 10), (Status.SCREENING, 5)])
    app2 = pipeline(session, company="B", statuses=[(Status.APPLIED, 10)])
    app1.resume_document_id = doc_a.id
    app2.resume_document_id = doc_b.id
    session.flush()
    stats = {g.label: g for g in analytics.by_resume(session)}
    assert stats["v1"].positive_rate == 1.0
    assert stats["v2"].positive_rate == 0.0


def test_by_source(session: Session) -> None:
    app1 = pipeline(session, company="A", statuses=[(Status.APPLIED, 9), (Status.SCREENING, 3)])
    app2 = pipeline(session, company="B", statuses=[(Status.APPLIED, 9)])
    app1.source = "referral"
    app2.source = "linkedin"
    session.flush()
    stats = {g.label: g for g in analytics.by_source(session)}
    assert stats["referral"].positive == 1
    assert stats["linkedin"].positive == 0


def test_time_to_apply_buckets(session: Session) -> None:
    co = make_company(session, "A")
    fast_job = make_job(session, co, "Fast", posted_at=(utcnow() - dt.timedelta(days=10)).date())
    slow_job = make_job(session, co, "Slow", posted_at=(utcnow() - dt.timedelta(days=40)).date())
    fast = make_app(session, fast_job)
    slow = make_app(session, slow_job)
    add_status(session, fast, Status.APPLIED, days_ago=9)  # 1 day after posting
    add_status(session, fast, Status.SCREENING, days_ago=2)
    add_status(session, slow, Status.APPLIED, days_ago=20)  # 20 days after posting
    stats = {g.label: g for g in analytics.by_time_to_apply(session)}
    assert stats["0-1d"].applications == 1
    assert stats["0-1d"].positive == 1
    assert stats["15d+"].applications == 1
    assert stats["15d+"].positive == 0


def test_skill_gap_against_resume_text(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 3)])
    doc = Document(
        type="resume",
        label="v3",
        file_path="x",
        content_hash="h",
        extracted_text="Seasoned Python developer; PostgreSQL, Docker.",
    )
    session.add(doc)
    session.add(JobSkill(job_id=app.job_id, skill="python", evidence="Python required"))
    session.add(JobSkill(job_id=app.job_id, skill="kubernetes", evidence="K8s deploys"))
    session.flush()
    stats, resume_label, jobs_covered = analytics.skill_gap(session)
    assert resume_label == "v3"
    assert jobs_covered == 1
    by_name = {s.skill: s for s in stats}
    assert by_name["python"].in_resume
    assert not by_name["kubernetes"].in_resume
    assert by_name["kubernetes"].pct == 1.0


def test_briefing_sections(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 30)])  # stale vs 14d SLA
    assert app.id is not None
    session.add(
        Interview(
            application_id=app.id,
            round=1,
            scheduled_at=utcnow() + dt.timedelta(hours=20),
            format="video",
        )
    )
    session.add(
        TaskItem(
            application_id=app.id,
            due_at=utcnow() - dt.timedelta(hours=2),
            description="send thank-you note",
        )
    )
    session.flush()
    brief = analytics.briefing(session, {"applied": 14})
    assert len(brief.interviews_next_48h) == 1
    assert len(brief.tasks_due) == 1 and brief.tasks_due[0].overdue
    assert len(brief.stale) == 1
    # the interview counts as activity? No — interviews aren't in last_activity,
    # but the applied event 30 days ago is, so it's stale.
    assert brief.stale[0].days_quiet >= 29
