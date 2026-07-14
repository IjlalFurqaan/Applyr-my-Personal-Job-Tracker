from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from jobtrack.config import Config
from jobtrack.core import proposals as props
from jobtrack.core.clock import utcnow
from jobtrack.core.enums import EmailClass, ProposalStatus, Status
from jobtrack.core.events import derived_status
from jobtrack.core.models import Email
from jobtrack.ingest.linker import link_email, process_new, sender_domain
from tests.conftest import add_status, make_app, make_company, make_job, pipeline
from tests.fakes import FakeProvider


def _email(
    session: Session,
    sender: str,
    subject: str,
    *,
    thread_id: str | None = None,
    message_id: str | None = None,
) -> Email:
    email = Email(
        message_id=message_id or f"<{subject}-{sender}>",
        thread_id=thread_id,
        sender=sender,
        subject=subject,
        received_at=utcnow() - dt.timedelta(hours=1),
        body_text="body",
    )
    session.add(email)
    session.flush()
    return email


def test_sender_domain_parsing() -> None:
    assert sender_domain("Jane Doe <jane@stripe.com>") == "stripe.com"
    assert sender_domain("no-reply@mail.Greenhouse.IO") == "mail.greenhouse.io"
    assert sender_domain("garbage") is None


def test_domain_link_single_application(session: Session) -> None:
    app = pipeline(session, domain="stripe.com", statuses=[(Status.APPLIED, 5)])
    email = _email(session, "recruiting@stripe.com", "Your application at Stripe")
    result = link_email(session, email)
    assert result.application_id == app.id


def test_subdomain_still_links(session: Session) -> None:
    app = pipeline(session, domain="stripe.com", statuses=[(Status.APPLIED, 5)])
    email = _email(session, "no-reply@mail.stripe.com", "Update")
    assert link_email(session, email).application_id == app.id


def test_freemail_never_links_by_domain(session: Session) -> None:
    pipeline(session, domain="gmail.com", statuses=[(Status.APPLIED, 5)])
    email = _email(session, "someone@gmail.com", "hi")
    assert link_email(session, email).application_id is None


def test_subject_tokens_pick_between_two_applications(session: Session) -> None:
    co = make_company(session, "Stripe", domain="stripe.com")
    backend = make_app(session, make_job(session, co, "Backend Engineer"))
    data = make_app(session, make_job(session, co, "Data Scientist"))
    add_status(session, backend, Status.APPLIED, days_ago=5)
    add_status(session, data, Status.APPLIED, days_ago=5)
    email = _email(session, "r@stripe.com", "Backend Engineer — next steps")
    assert link_email(session, email).application_id == backend.id


def test_thread_continuity_links_even_from_freemail(session: Session) -> None:
    app = pipeline(session, domain="stripe.com", statuses=[(Status.APPLIED, 5)])
    first = _email(session, "r@stripe.com", "Interview", thread_id="<root@stripe>")
    first.linked_application_id = app.id
    session.flush()
    followup = _email(
        session, "recruiter@gmail.com", "Re: Interview", thread_id="<root@stripe>"
    )
    result = link_email(session, followup)
    assert result.application_id == app.id
    assert "thread" in result.reason


def test_process_new_creates_pending_proposal_never_commits(
    session: Session, config: Config
) -> None:
    app = pipeline(session, domain="stripe.com", statuses=[(Status.APPLIED, 5)])
    assert app.id is not None
    _email(session, "recruiting@stripe.com", "Your Stripe application")
    provider = FakeProvider(
        chat_responses=['{"classification": "rejection", "confidence": 0.95}']
    )
    created = process_new(session, provider, config)
    assert len(created) == 1
    proposal = created[0]
    assert proposal.status == ProposalStatus.PENDING.value
    assert proposal.source == "email"
    # nothing committed: still applied
    assert derived_status(session, app.id) is Status.APPLIED
    assert proposal.id is not None
    props.confirm(session, proposal.id)
    assert derived_status(session, app.id) is Status.REJECTED


def test_low_confidence_stores_classification_but_no_proposal(
    session: Session, config: Config
) -> None:
    pipeline(session, domain="stripe.com", statuses=[(Status.APPLIED, 5)])
    email = _email(session, "recruiting@stripe.com", "Your Stripe application")
    provider = FakeProvider(
        chat_responses=['{"classification": "rejection", "confidence": 0.4}']
    )
    created = process_new(session, provider, config)
    assert created == []
    assert email.classification == EmailClass.REJECTION.value
    assert email.processed_at is not None


def test_invite_when_already_interviewing_becomes_interaction(
    session: Session, config: Config
) -> None:
    pipeline(
        session,
        domain="stripe.com",
        statuses=[(Status.APPLIED, 9), (Status.FINAL_ROUND, 1)],
    )
    _email(session, "recruiting@stripe.com", "Interview scheduling")
    provider = FakeProvider(
        chat_responses=['{"classification": "interview_invite", "confidence": 0.9}']
    )
    created = process_new(session, provider, config)
    assert len(created) == 1
    action = props.load_action(created[0])
    assert action.kind == "log_interaction"  # no backwards move proposed


def test_unlinked_email_is_processed_without_proposal(
    session: Session, config: Config
) -> None:
    email = _email(session, "recruiter@unknown-agency.com", "Exciting opportunity")
    provider = FakeProvider(
        chat_responses=['{"classification": "recruiter_outreach", "confidence": 0.9}']
    )
    created = process_new(session, provider, config)
    assert created == []
    assert email.processed_at is not None
    assert email.linked_application_id is None
