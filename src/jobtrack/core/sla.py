"""Ghosted/stale are derived at query time from last activity vs per-stage SLAs.

They are never stored. `ghosted` = no employer signal past the SLA while sitting
in an early stage (applied/screening); `stale` = the same idea for any
non-terminal stage.
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from jobtrack.core.clock import utcnow
from jobtrack.core.enums import TERMINAL_STATUSES, Status
from jobtrack.core.events import derived_status, last_activity

GHOSTABLE_STATUSES: frozenset[Status] = frozenset({Status.APPLIED, Status.SCREENING})


def days_since_activity(
    session: Session, application_id: int, *, now: dt.datetime | None = None
) -> int | None:
    last = last_activity(session, application_id)
    if last is None:
        return None
    return max(((now or utcnow()) - last).days, 0)


def is_stale(
    session: Session,
    application_id: int,
    sla_days: dict[str, int],
    *,
    now: dt.datetime | None = None,
) -> bool:
    status = derived_status(session, application_id)
    if status is None or status in TERMINAL_STATUSES:
        return False
    limit = sla_days.get(status.value)
    if limit is None:
        return False
    days = days_since_activity(session, application_id, now=now)
    return days is not None and days > limit


def is_ghosted(
    session: Session,
    application_id: int,
    sla_days: dict[str, int],
    *,
    now: dt.datetime | None = None,
) -> bool:
    status = derived_status(session, application_id)
    if status not in GHOSTABLE_STATUSES:
        return False
    return is_stale(session, application_id, sla_days, now=now)
