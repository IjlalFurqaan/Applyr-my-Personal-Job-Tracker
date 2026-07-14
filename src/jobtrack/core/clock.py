"""Time helpers. All datetimes are stored as naive UTC; rendered local at the edges."""

import datetime as dt


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def to_local(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC).astimezone()
