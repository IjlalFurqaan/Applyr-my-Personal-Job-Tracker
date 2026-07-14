from __future__ import annotations

from sqlmodel import Session

from jobtrack.core.clock import utcnow
from jobtrack.core.enums import Status
from jobtrack.core.events import append_note_event
from jobtrack.core.sla import is_ghosted, is_stale
from tests.conftest import pipeline

SLA = {"applied": 14, "screening": 10, "interviewing": 10}


def test_quiet_past_sla_is_stale_and_ghosted(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 20)])
    assert app.id is not None
    assert is_stale(session, app.id, SLA)
    assert is_ghosted(session, app.id, SLA)


def test_exactly_at_sla_is_not_stale(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 14)])
    assert app.id is not None
    assert not is_stale(session, app.id, SLA)


def test_interviewing_can_be_stale_but_not_ghosted(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 40), (Status.INTERVIEWING, 15)])
    assert app.id is not None
    assert is_stale(session, app.id, SLA)
    assert not is_ghosted(session, app.id, SLA)


def test_terminal_statuses_are_never_stale(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 60), (Status.REJECTED, 40)])
    assert app.id is not None
    assert not is_stale(session, app.id, SLA)
    assert not is_ghosted(session, app.id, SLA)


def test_recent_activity_resets_the_clock(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 20)])
    assert app.id is not None
    assert is_stale(session, app.id, SLA)
    append_note_event(session, app.id, "they replied", occurred_at=utcnow())
    assert not is_stale(session, app.id, SLA)


def test_stage_without_sla_never_stale(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.SAVED, 90)])
    assert app.id is not None
    assert not is_stale(session, app.id, SLA)
