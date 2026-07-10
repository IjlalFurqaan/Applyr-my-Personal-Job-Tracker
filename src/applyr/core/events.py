"""Event sourcing: append events, derive status. The only way state changes.

`application_events` is append-only. Status is always the `to_status` of the
latest status_change event ordered by (occurred_at, id) — so backfilled events
land in the right place and insertion order never wins over occurred_at.

`from_status` is recorded at append time for the diff/audit trail; it can be
historically wrong after a backfill, which is fine — derivation never uses it.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlmodel import Session, col, select

from applyr.core.clock import utcnow
from applyr.core.enums import EventSource, EventType, Status
from applyr.core.models import Application, ApplicationEvent, Email, Interaction


def status_events(session: Session, application_id: int) -> list[ApplicationEvent]:
    stmt = (
        select(ApplicationEvent)
        .where(
            ApplicationEvent.application_id == application_id,
            ApplicationEvent.type == EventType.STATUS_CHANGE.value,
        )
        .order_by(col(ApplicationEvent.occurred_at), col(ApplicationEvent.id))
    )
    return list(session.exec(stmt).all())


def _effective_status_events(session: Session, application_id: int) -> list[ApplicationEvent]:
    """Status events for derivation.

    `saved` is the pre-application holding state — you are never "saved" once the
    application has moved forward. So once any non-saved status event exists, the
    auto-generated `saved` entry event is excluded. This keeps derivation correct
    when a real application is backfilled with an earlier date than the (wall-clock
    `now`) save event, e.g. "I actually applied to Stripe last week."
    """
    events = status_events(session, application_id)
    non_saved = [ev for ev in events if ev.to_status != Status.SAVED.value]
    return non_saved or events


def derived_status(session: Session, application_id: int) -> Status | None:
    events = _effective_status_events(session, application_id)
    if not events:
        return None
    last = events[-1]
    return Status(last.to_status) if last.to_status else None


def days_in_stage(
    session: Session, application_id: int, *, now: dt.datetime | None = None
) -> int | None:
    events = _effective_status_events(session, application_id)
    if not events:
        return None
    now = now or utcnow()
    return max((now - events[-1].occurred_at).days, 0)


def first_reached(session: Session, application_id: int, status: Status) -> dt.datetime | None:
    for ev in status_events(session, application_id):
        if ev.to_status == status.value:
            return ev.occurred_at
    return None


def append_status_event(
    session: Session,
    application_id: int,
    to_status: Status,
    *,
    occurred_at: dt.datetime | None = None,
    source: EventSource = EventSource.MANUAL,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ApplicationEvent:
    current = derived_status(session, application_id)
    body: dict[str, Any] = dict(payload or {})
    if note:
        body["note"] = note
    ev = ApplicationEvent(
        application_id=application_id,
        type=EventType.STATUS_CHANGE.value,
        from_status=current.value if current else None,
        to_status=to_status.value,
        occurred_at=occurred_at or utcnow(),
        payload_json=json.dumps(body) if body else None,
        source=source.value,
    )
    session.add(ev)
    # Denormalisation-free bookkeeping: applied_at is a capture-time fact on the
    # application (used for time-to-apply analytics), set once.
    if to_status is Status.APPLIED:
        app = session.get(Application, application_id)
        if app is not None and app.applied_at is None:
            app.applied_at = (occurred_at or utcnow()).date()
            session.add(app)
    session.flush()
    return ev


def append_note_event(
    session: Session,
    application_id: int,
    text: str,
    *,
    occurred_at: dt.datetime | None = None,
    source: EventSource = EventSource.MANUAL,
) -> ApplicationEvent:
    ev = ApplicationEvent(
        application_id=application_id,
        type=EventType.NOTE.value,
        occurred_at=occurred_at or utcnow(),
        payload_json=json.dumps({"note": text}),
        source=source.value,
    )
    session.add(ev)
    session.flush()
    return ev


def last_activity(session: Session, application_id: int) -> dt.datetime | None:
    """Latest of: any event, any interaction, any linked email."""
    candidates: list[dt.datetime] = []
    ev = session.exec(
        select(ApplicationEvent.occurred_at)
        .where(ApplicationEvent.application_id == application_id)
        .order_by(col(ApplicationEvent.occurred_at).desc())
        .limit(1)
    ).first()
    if ev is not None:
        candidates.append(ev)
    inter = session.exec(
        select(Interaction.occurred_at)
        .where(Interaction.application_id == application_id)
        .order_by(col(Interaction.occurred_at).desc())
        .limit(1)
    ).first()
    if inter is not None:
        candidates.append(inter)
    mail = session.exec(
        select(Email.received_at)
        .where(Email.linked_application_id == application_id)
        .order_by(col(Email.received_at).desc())
        .limit(1)
    ).first()
    if mail is not None:
        candidates.append(mail)
    return max(candidates) if candidates else None


def notes_for(session: Session, application_id: int) -> list[tuple[dt.datetime, str]]:
    """(occurred_at, text) for all note events and noted status changes, oldest first."""
    stmt = (
        select(ApplicationEvent)
        .where(ApplicationEvent.application_id == application_id)
        .order_by(col(ApplicationEvent.occurred_at), col(ApplicationEvent.id))
    )
    out: list[tuple[dt.datetime, str]] = []
    for ev in session.exec(stmt).all():
        if not ev.payload_json:
            continue
        note = json.loads(ev.payload_json).get("note")
        if note:
            out.append((ev.occurred_at, str(note)))
    return out
