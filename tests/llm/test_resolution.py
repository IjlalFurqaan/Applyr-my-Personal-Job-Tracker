"""The resolution matrix from PLAN.md §7 — the ambiguous cases especially."""

from __future__ import annotations

from sqlmodel import Session

from jobtrack.core.enums import Status
from jobtrack.core.models import CompanyAlias, Contact
from jobtrack.llm.resolution import (
    resolve_application,
    resolve_company,
    resolve_contact,
    resolve_job,
)
from tests.conftest import add_status, make_app, make_company, make_job, pipeline


def test_exact_company(session: Session) -> None:
    co = make_company(session, "Stripe")
    result = resolve_company(session, "Stripe")
    assert result.outcome == "resolved" and result.entity_id == co.id


def test_case_and_legal_suffix_noise(session: Session) -> None:
    co = make_company(session, "Stripe")
    result = resolve_company(session, "STRIPE, Inc.")
    assert result.outcome == "resolved" and result.entity_id == co.id


def test_alias_resolves(session: Session) -> None:
    meta = make_company(session, "Meta")
    assert meta.id is not None
    session.add(CompanyAlias(company_id=meta.id, alias="Facebook"))
    session.flush()
    result = resolve_company(session, "Facebook")
    assert result.outcome == "resolved" and result.entity_id == meta.id


def test_typo_disambiguates_instead_of_guessing(session: Session) -> None:
    make_company(session, "Stripe")
    result = resolve_company(session, "Strpie")
    assert result.outcome == "ambiguous"
    assert [c.label for c in result.candidates] == ["Stripe"]


def test_substring_company_names_do_not_collide(session: Session) -> None:
    meta = make_company(session, "Meta")
    metabase = make_company(session, "Metabase")
    assert resolve_company(session, "Meta").entity_id == meta.id
    assert resolve_company(session, "Metabase").entity_id == metabase.id


def test_subset_names_are_ambiguous(session: Session) -> None:
    make_company(session, "Databricks")
    make_company(session, "Databricks Labs")
    result = resolve_company(session, "Databricks")
    assert result.outcome == "ambiguous"
    assert len(result.candidates) == 2


def test_unknown_company_not_found(session: Session) -> None:
    make_company(session, "Stripe")
    result = resolve_company(session, "Umbrella Corp")
    assert result.outcome == "not_found"


def test_explicit_ref_wins(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 2)])
    result = resolve_application(session, f"app#{app.id}")
    assert result.outcome == "resolved" and result.entity_id == app.id


def test_two_active_applications_disambiguate(session: Session) -> None:
    co = make_company(session, "Stripe")
    app1 = make_app(session, make_job(session, co, "Backend Engineer"))
    app2 = make_app(session, make_job(session, co, "Data Scientist"))
    add_status(session, app1, Status.APPLIED, days_ago=5)
    add_status(session, app2, Status.APPLIED, days_ago=3)
    result = resolve_application(session, "I heard back from Stripe")
    assert result.outcome == "ambiguous"
    assert {c.ref for c in result.candidates} == {f"app#{app1.id}", f"app#{app2.id}"}


def test_token_narrowing_picks_the_right_role(session: Session) -> None:
    co = make_company(session, "Stripe")
    backend = make_app(session, make_job(session, co, "Backend Engineer"))
    other = make_app(session, make_job(session, co, "Data Scientist"))
    add_status(session, backend, Status.APPLIED, days_ago=5)
    add_status(session, other, Status.APPLIED, days_ago=3)
    result = resolve_application(session, "the Stripe backend role")
    assert result.outcome == "resolved" and result.entity_id == backend.id


def test_single_active_among_closed_resolves_with_note(session: Session) -> None:
    co = make_company(session, "Stripe")
    closed = make_app(session, make_job(session, co, "Backend Engineer"))
    active = make_app(session, make_job(session, co, "Data Scientist"))
    add_status(session, closed, Status.APPLIED, days_ago=30)
    add_status(session, closed, Status.REJECTED, days_ago=10)
    add_status(session, active, Status.APPLIED, days_ago=5)
    result = resolve_application(session, "Stripe")
    assert result.outcome == "resolved" and result.entity_id == active.id
    assert result.note is not None  # the assumption is surfaced


def test_company_without_applications(session: Session) -> None:
    make_company(session, "Stripe")
    result = resolve_application(session, "Stripe")
    assert result.outcome == "not_found"
    assert "no applications" in (result.hint or "")


def test_archived_only_hints_at_archived(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 40)])
    app.archived = True
    session.flush()
    result = resolve_application(session, "Stripe")
    assert result.outcome == "not_found"
    assert "archived" in (result.hint or "")


def test_ambiguous_company_propagates_to_application(session: Session) -> None:
    make_company(session, "Databricks")
    make_company(session, "Databricks Labs")
    result = resolve_application(session, "Databricks")
    assert result.outcome == "ambiguous"
    assert all(c.ref.startswith("co#") for c in result.candidates)


def test_resolve_job_prefers_unapplied(session: Session) -> None:
    co = make_company(session, "Stripe")
    applied_job = make_job(session, co, "Backend Engineer")
    saved_job = make_job(session, co, "Platform Engineer")
    applied_app = make_app(session, applied_job)
    saved_app = make_app(session, saved_job)
    add_status(session, applied_app, Status.APPLIED, days_ago=5)
    add_status(session, saved_app, Status.SAVED, days_ago=2)
    result = resolve_job(session, "Stripe")
    assert result.outcome == "resolved" and result.entity_id == saved_job.id
    assert result.note is not None


def test_contact_fuzzy_and_ambiguous(session: Session) -> None:
    session.add(Contact(name="Jana Mueller"))
    session.add(Contact(name="Jana Schmidt"))
    session.flush()
    result = resolve_contact(session, "Jana")
    assert result.outcome == "ambiguous"
    assert len(result.candidates) == 2
    exact = resolve_contact(session, "Jana Mueller")
    assert exact.outcome == "resolved"
