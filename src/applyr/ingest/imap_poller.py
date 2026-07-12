"""Read-only IMAP polling. Never marks read, never moves, never deletes,
never sends. Credentials live in the OS keyring (Windows Credential Manager),
never in config or DB. UID checkpoints live in the meta table."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

import keyring
from sqlmodel import Session, select

from applyr.config import Config
from applyr.core.models import Email, Meta

KEYRING_SERVICE = "applyr-imap"


class IngestError(Exception):
    pass


def store_password(user: str, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, user, password)


def get_password(user: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, user)


def _checkpoint_key(config: Config) -> str:
    return f"imap_uid:{config.email.host}:{config.email.user}:{config.email.folder}"


def _get_checkpoint(session: Session, key: str) -> int:
    row = session.get(Meta, key)
    return int(row.value) if row is not None else 0


def _set_checkpoint(session: Session, key: str, uid: int) -> None:
    row = session.get(Meta, key)
    if row is None:
        row = Meta(key=key, value=str(uid))
    else:
        row.value = str(uid)
    session.add(row)
    session.flush()


def _thread_id(headers: Mapping[str, tuple[str, ...]], message_id: str) -> str:
    """First message-id in References (thread root), else In-Reply-To, else self."""
    refs = " ".join(headers.get("references", ()))
    ids = refs.split()
    if ids:
        return ids[0].strip()
    reply_to = " ".join(headers.get("in-reply-to", ())).strip()
    return reply_to or message_id


def _to_naive_utc(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return dt.datetime.now(dt.UTC).replace(tzinfo=None)
    if value.tzinfo is None:
        return value
    return value.astimezone(dt.UTC).replace(tzinfo=None)


def poll(session: Session, config: Config) -> int:
    """Fetch new messages into the emails table. Returns how many were stored."""
    from imap_tools import MailBox

    user = config.email.user
    if not user:
        raise IngestError("no email account configured — run `applyr email setup` first")
    password = get_password(user)
    if password is None:
        raise IngestError(f"no password in keyring for {user} — run `applyr email setup`")

    key = _checkpoint_key(config)
    last_uid = _get_checkpoint(session, key)
    stored = 0
    max_uid = last_uid

    with MailBox(config.email.host).login(
        user, password, initial_folder=config.email.folder
    ) as mailbox:
        criteria = f"UID {last_uid + 1}:*" if last_uid else "ALL"
        for msg in mailbox.fetch(criteria, mark_seen=False):
            uid = int(msg.uid) if msg.uid else 0
            if uid <= last_uid:
                continue  # servers return the last-seen UID for open-ended ranges
            max_uid = max(max_uid, uid)
            message_id = " ".join(msg.headers.get("message-id", ())).strip() or f"uid:{uid}"
            existing = session.exec(
                select(Email).where(Email.message_id == message_id)
            ).first()
            if existing is not None:
                continue
            body = msg.text or ""
            if not body and msg.html:
                from markdownify import markdownify

                body = str(markdownify(msg.html))
            session.add(
                Email(
                    message_id=message_id,
                    thread_id=_thread_id(msg.headers, message_id),
                    sender=msg.from_,
                    subject=msg.subject or "",
                    received_at=_to_naive_utc(msg.date),
                    body_text=body[:20000],
                )
            )
            stored += 1
        session.flush()

    if max_uid > last_uid:
        _set_checkpoint(session, key, max_uid)
    return stored
