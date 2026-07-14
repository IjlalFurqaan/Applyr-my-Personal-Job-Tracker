from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from jobtrack.core import actions as act
from jobtrack.core import proposals as props
from jobtrack.core.actions import is_auto_approvable
from jobtrack.core.enums import ProposalStatus, Status
from jobtrack.core.events import derived_status
from jobtrack.core.models import Application, Company, Document, Job
from jobtrack.core.proposals import ProposalError
from tests.conftest import pipeline


def test_propose_writes_nothing_but_the_proposal(session: Session) -> None:
    action = act.AddJob(new_company=act.NewCompany(name="Stripe"), title="Backend Engineer")
    proposal = props.propose(session, action, source="cli")
    assert proposal.status == ProposalStatus.PENDING.value
    assert session.exec(select(Company)).first() is None
    assert session.exec(select(Job)).first() is None


def test_confirm_add_job_creates_company_job_and_saved_app(session: Session) -> None:
    action = act.AddJob(
        new_company=act.NewCompany(name="Stripe", domain="stripe.com"),
        title="Backend Engineer",
        jd_markdown="# JD\nkubernetes required",
        jd_hash="abc123",
    )
    proposal = props.propose(session, action)
    assert proposal.id is not None
    result = props.confirm(session, proposal.id)
    assert set(result.refs) == {"company", "job", "application"}
    app = session.get(Application, result.refs["application"])
    assert app is not None
    assert derived_status(session, result.refs["application"]) is Status.SAVED
    job = session.get(Job, result.refs["job"])
    assert job is not None and job.jd_markdown is not None  # immutable snapshot stored


def test_confirm_twice_is_an_error(session: Session) -> None:
    action = act.AddCompany(company=act.NewCompany(name="Stripe"))
    proposal = props.propose(session, action)
    assert proposal.id is not None
    props.confirm(session, proposal.id)
    with pytest.raises(ProposalError):
        props.confirm(session, proposal.id)


def test_reject_writes_nothing(session: Session) -> None:
    action = act.AddCompany(company=act.NewCompany(name="Stripe"))
    proposal = props.propose(session, action)
    assert proposal.id is not None
    props.reject(session, proposal.id, "changed my mind")
    assert session.exec(select(Company)).first() is None
    assert proposal.status == ProposalStatus.REJECTED.value
    with pytest.raises(ProposalError):
        props.confirm(session, proposal.id)


def test_update_status_appends_event(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 5)])
    assert app.id is not None
    action = act.UpdateStatus(application_id=app.id, to_status=Status.SCREENING)
    proposal = props.propose(session, action)
    assert proposal.id is not None
    props.confirm(session, proposal.id)
    assert derived_status(session, app.id) is Status.SCREENING


def test_log_application_reuses_saved_app_and_pins_resume(
    session: Session, tmp_path: Path
) -> None:
    app = pipeline(session, statuses=[(Status.SAVED, 3)])
    assert app.id is not None
    resume = tmp_path / "resume-v2.txt"
    resume.write_text("python kubernetes go", encoding="utf-8")
    action = act.LogApplication(
        job_id=app.job_id,
        new_resume=act.NewDocument(file_path=str(resume), label="resume-v2"),
        source="referral",
    )
    proposal = props.propose(session, action)
    assert proposal.id is not None
    result = props.confirm(session, proposal.id)
    # same application row, not a second one
    assert result.refs["application"] == app.id
    assert len(session.exec(select(Application)).all()) == 1
    assert derived_status(session, app.id) is Status.APPLIED
    assert app.applied_at is not None
    doc = session.get(Document, result.refs["resume_document"])
    assert doc is not None and len(doc.content_hash) == 64
    assert app.resume_document_id == doc.id
    assert app.source == "referral"


def test_unusual_transition_is_flagged_in_diff(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 9), (Status.REJECTED, 2)])
    assert app.id is not None
    action = act.UpdateStatus(application_id=app.id, to_status=Status.INTERVIEWING)
    proposal = props.propose(session, action)
    diff = props.diff_for(session, proposal)
    assert "unusual" in diff
    assert proposal.id is not None
    props.confirm(session, proposal.id)  # warned, not forbidden
    assert derived_status(session, app.id) is Status.INTERVIEWING


def test_auto_approve_floor_is_hard(session: Session) -> None:
    # even if the user's config lists update_status, it must not be eligible
    assert is_auto_approvable("add_note", ("add_note", "update_status"))
    assert not is_auto_approvable("update_status", ("add_note", "update_status"))
    assert not is_auto_approvable("log_application", ("log_application",))


def test_commit_error_when_target_vanished(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 5)])
    assert app.id is not None
    action = act.UpdateStatus(application_id=9999, to_status=Status.SCREENING)
    proposal = props.propose(session, action)
    assert proposal.id is not None
    with pytest.raises(props.CommitError):
        props.confirm(session, proposal.id)
