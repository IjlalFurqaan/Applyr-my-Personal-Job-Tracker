from __future__ import annotations

import datetime as dt
import json

from sqlmodel import Session

from jobtrack.core.clock import utcnow
from jobtrack.core.enums import Status
from jobtrack.core.events import append_note_event
from jobtrack.core.models import Document, Interview
from jobtrack.core.prep import build_dossier, question_bank
from tests.conftest import make_app, make_company, make_job, pipeline


def _full_pipeline(session: Session) -> tuple[int, int]:
    co = make_company(session, "Stripe")
    co.industry = "payments"
    co.notes = "Series I fintech, remote-friendly."
    session.flush()
    job = make_job(
        session,
        co,
        "Backend Engineer",
        jd_markdown="# Backend Engineer\n\nKubernetes, Go, and payments experience required.",
    )
    doc = Document(
        type="resume",
        label="resume-v3",
        file_path="/x/resume.pdf",
        content_hash="abc123def456",
        extracted_text="Go developer with Kubernetes and distributed systems experience.",
    )
    session.add(doc)
    session.flush()
    app = make_app(session, job, resume_document_id=doc.id)
    assert app.id is not None and job.id is not None
    return app.id, job.id


def test_dossier_assembles_all_inputs(session: Session) -> None:
    app_id, _ = _full_pipeline(session)
    # a past debrief with questions
    session.add(
        Interview(
            application_id=app_id,
            round=1,
            scheduled_at=utcnow() - dt.timedelta(days=3),
            format="phone",
            interviewers_json=json.dumps(["Jana Mueller"]),
            debrief_notes="Went well, asked about system design.",
            questions_asked_json=json.dumps(["Design a rate limiter", "Why us?"]),
            outcome="advanced",
        )
    )
    # an upcoming interview
    session.add(
        Interview(
            application_id=app_id,
            round=2,
            scheduled_at=utcnow() + dt.timedelta(days=2),
            format="video",
            interviewers_json=json.dumps(["Sam Lee"]),
        )
    )
    append_note_event(session, app_id, "Referred by a friend on the platform team.")
    session.flush()

    dossier = build_dossier(session, app_id)
    # JD archived
    assert "Kubernetes, Go, and payments" in dossier
    # resume actually sent
    assert "resume-v3" in dossier
    assert "distributed systems" in dossier
    # company notes
    assert "Series I fintech" in dossier
    # previous round + questions
    assert "Design a rate limiter" in dossier
    assert "advanced" in dossier
    # upcoming interview + interviewer
    assert "Round 2" in dossier
    assert "Sam Lee" in dossier
    # note
    assert "Referred by a friend" in dossier


def test_dossier_handles_missing_pieces(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 2)])
    assert app.id is not None
    dossier = build_dossier(session, app.id)
    assert "not captured" in dossier  # no JD
    assert "none pinned" in dossier  # no resume


def test_question_bank_accumulates(session: Session) -> None:
    app_id, _ = _full_pipeline(session)
    session.add(
        Interview(
            application_id=app_id,
            round=1,
            scheduled_at=utcnow() - dt.timedelta(days=5),
            format="video",
            questions_asked_json=json.dumps(["Tell me about a hard bug", "CAP theorem?"]),
        )
    )
    session.flush()
    bank = question_bank(session)
    questions = [q for _, _, q in bank]
    assert "Tell me about a hard bug" in questions
    assert "CAP theorem?" in questions
    assert all(company == "Stripe" for company, _, _ in bank)
