from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from applyr.core.clock import utcnow
from applyr.core.enums import Status
from applyr.core.events import (
    append_note_event,
    append_status_event,
    days_in_stage,
    derived_status,
    first_reached,
    last_activity,
)
from applyr.core.models import Interaction
from tests.conftest import add_status, pipeline


def test_derived_status_follows_latest_event(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.SAVED, 10), (Status.APPLIED, 8)])
    assert app.id is not None
    assert derived_status(session, app.id) is Status.APPLIED


def test_no_events_means_no_status(session: Session) -> None:
    app = pipeline(session)
    assert app.id is not None
    assert derived_status(session, app.id) is None
    assert days_in_stage(session, app.id) is None


def test_saved_ignored_once_application_progresses(session: Session) -> None:
    # Real backfill: job saved *now*, but the application happened days earlier.
    app = pipeline(session)
    assert app.id is not None
    add_status(session, app, Status.SAVED, days_ago=0)  # save event at "now"
    add_status(session, app, Status.APPLIED, days_ago=11)  # backdated apply
    add_status(session, app, Status.SCREENING, days_ago=6)  # backdated screening
    # saved is newest by wall-clock, but must not win once progressed.
    assert derived_status(session, app.id) is Status.SCREENING
    # days_in_stage measures the effective (non-saved) latest event.
    assert days_in_stage(session, app.id) == 6


def test_saved_is_status_when_only_event(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.SAVED, 3)])
    assert app.id is not None
    assert derived_status(session, app.id) is Status.SAVED


def test_backfilled_event_does_not_override_newer(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 10), (Status.REJECTED, 1)])
    assert app.id is not None
    # Backfill a screening event that happened 5 days ago (between the two).
    add_status(session, app, Status.SCREENING, days_ago=5)
    assert derived_status(session, app.id) is Status.REJECTED


def test_out_of_order_insertion_orders_by_occurred_at(session: Session) -> None:
    app = pipeline(session)
    assert app.id is not None
    add_status(session, app, Status.SCREENING, days_ago=2)  # inserted first
    add_status(session, app, Status.APPLIED, days_ago=9)  # inserted later, older
    assert derived_status(session, app.id) is Status.SCREENING


def test_same_timestamp_tie_broken_by_insertion_id(session: Session) -> None:
    app = pipeline(session)
    assert app.id is not None
    ts = utcnow() - dt.timedelta(days=1)
    append_status_event(session, app.id, Status.APPLIED, occurred_at=ts)
    append_status_event(session, app.id, Status.SCREENING, occurred_at=ts)
    assert derived_status(session, app.id) is Status.SCREENING


def test_days_in_stage(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 30), (Status.SCREENING, 7)])
    assert app.id is not None
    assert days_in_stage(session, app.id) == 7


def test_applied_event_sets_applied_at_once(session: Session) -> None:
    app = pipeline(session)
    assert app.id is not None
    first = utcnow() - dt.timedelta(days=9)
    append_status_event(session, app.id, Status.APPLIED, occurred_at=first)
    assert app.applied_at == first.date()
    # A correcting re-application later must not move the original date.
    append_status_event(session, app.id, Status.APPLIED)
    assert app.applied_at == first.date()


def test_from_status_recorded_at_append(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 5)])
    assert app.id is not None
    ev = append_status_event(session, app.id, Status.SCREENING)
    assert ev.from_status == Status.APPLIED.value
    assert ev.to_status == Status.SCREENING.value


def test_first_reached(session: Session) -> None:
    app = pipeline(
        session,
        statuses=[(Status.APPLIED, 10), (Status.SCREENING, 6), (Status.INTERVIEWING, 2)],
    )
    assert app.id is not None
    reached = first_reached(session, app.id, Status.SCREENING)
    assert reached is not None
    assert abs((utcnow() - reached).days - 6) <= 1


def test_last_activity_includes_notes_and_interactions(session: Session) -> None:
    app = pipeline(session, statuses=[(Status.APPLIED, 20)])
    assert app.id is not None
    before = last_activity(session, app.id)
    assert before is not None
    append_note_event(session, app.id, "pinged the recruiter")
    session.add(
        Interaction(application_id=app.id, summary="call", occurred_at=utcnow())
    )
    session.flush()
    after = last_activity(session, app.id)
    assert after is not None and after > before
