"""Link classified emails to applications and turn them into pending proposals.

Linking is deterministic: sender domain -> company, then thread continuity,
then subject/title token overlap. Nothing auto-commits — high-confidence
classifications become pending proposals in `applyr review`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlmodel import Session, select

from applyr.config import Config
from applyr.core import actions as act
from applyr.core import proposals as props
from applyr.core.clock import utcnow
from applyr.core.enums import (
    STATUS_ORDER,
    Direction,
    EmailClass,
    InteractionChannel,
    Status,
)
from applyr.core.events import derived_status
from applyr.core.models import Application, Email, Job, Proposal
from applyr.core.normalize import tokens
from applyr.core.repos import applications as apps_repo
from applyr.core.repos import companies as companies_repo
from applyr.ingest.classifier import classify
from applyr.llm.provider import LLMProvider

# Free-mail domains never identify a company.
FREEMAIL = frozenset(
    {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "gmx.de",
     "gmx.net", "web.de", "icloud.com", "proton.me", "protonmail.com"}
)

_ADDR = re.compile(r"<?([\w.+-]+)@([\w.-]+)>?")


def sender_domain(sender: str) -> str | None:
    m = _ADDR.search(sender)
    if m is None:
        return None
    return m.group(2).casefold()


@dataclass
class LinkResult:
    application_id: int | None
    reason: str


def link_email(session: Session, email: Email) -> LinkResult:
    # 1. Thread continuity: an earlier email in the same thread already linked.
    if email.thread_id:
        stmt = select(Email).where(
            Email.thread_id == email.thread_id,
            Email.id != email.id,
        )
        for other in session.exec(stmt).all():
            if other.linked_application_id is not None:
                return LinkResult(other.linked_application_id, "same thread as a linked email")

    # 2. Sender domain -> company.
    domain = sender_domain(email.sender)
    if domain is None or domain in FREEMAIL:
        return LinkResult(None, "sender domain unusable (missing or free-mail)")
    company = companies_repo.find_by_domain(session, domain)
    if company is None:
        return LinkResult(None, f"no company with domain {domain}")
    assert company.id is not None

    apps = apps_repo.for_company(session, company.id)
    if not apps:
        return LinkResult(None, f"no active applications at {company.name}")
    if len(apps) == 1:
        assert apps[0].id is not None
        return LinkResult(apps[0].id, f"only application at {company.name}")

    # 3. Subject tokens vs job titles.
    subject_tokens = set(tokens(email.subject))
    scored: list[tuple[Application, int]] = []
    for app in apps:
        job = session.get(Job, app.job_id)
        title_tokens = set(tokens(job.title)) if job else set()
        scored.append((app, len(subject_tokens & title_tokens)))
    best = max(score for _, score in scored)
    top = [app for app, score in scored if score == best]
    if best > 0 and len(top) == 1:
        assert top[0].id is not None
        return LinkResult(top[0].id, "subject matches job title")

    # 4. Fall back to the single non-terminal application, if there is one.
    non_terminal = [
        app
        for app in apps
        if app.id is not None
        and (derived_status(session, app.id) or Status.SAVED) not in
        {Status.ACCEPTED, Status.REJECTED, Status.WITHDRAWN}
    ]
    if len(non_terminal) == 1:
        assert non_terminal[0].id is not None
        return LinkResult(non_terminal[0].id, f"only active application at {company.name}")
    return LinkResult(None, f"{len(apps)} applications at {company.name}, cannot pick one")


def action_for(
    session: Session, email: Email, classification: EmailClass, application_id: int
) -> act.Action | None:
    """Map an email class to a proposed action, guarding against regressions."""
    note = f"email from {email.sender}: {email.subject}"
    status_map = {
        EmailClass.REJECTION: Status.REJECTED,
        EmailClass.INTERVIEW_INVITE: Status.INTERVIEWING,
        EmailClass.ASSESSMENT: Status.ASSESSMENT,
        EmailClass.OFFER: Status.OFFER,
    }
    if classification in status_map:
        to_status = status_map[classification]
        current = derived_status(session, application_id)
        # Don't propose moving backwards (e.g. an invite for round 3 when
        # already final_round) — log the touchpoint instead.
        if (
            to_status is not Status.REJECTED
            and current is not None
            and current in STATUS_ORDER
            and STATUS_ORDER[current] >= STATUS_ORDER[to_status]
        ):
            return act.LogInteraction(
                application_id=application_id,
                channel=InteractionChannel.EMAIL,
                direction=Direction.INBOUND,
                summary=note,
                occurred_at=email.received_at,
            )
        return act.UpdateStatus(
            application_id=application_id,
            to_status=to_status,
            occurred_at=email.received_at,
            note=note,
        )
    if classification in (EmailClass.RECRUITER_OUTREACH, EmailClass.SCHEDULING):
        return act.LogInteraction(
            application_id=application_id,
            channel=InteractionChannel.EMAIL,
            direction=Direction.INBOUND,
            summary=note,
            occurred_at=email.received_at,
        )
    return None


def process_new(session: Session, provider: LLMProvider, config: Config) -> list[Proposal]:
    """Classify + link every unprocessed email; return the proposals created."""
    created: list[Proposal] = []
    stmt = select(Email).where(Email.processed_at == None)  # noqa: E711
    for email in session.exec(stmt).all():
        result = classify(provider, email.sender, email.subject, email.body_text)
        email.classification = result.classification.value
        email.confidence = result.confidence

        link = link_email(session, email)
        email.linked_application_id = link.application_id

        if (
            result.classification is not EmailClass.IRRELEVANT
            and result.confidence >= config.email.confidence_threshold
            and link.application_id is not None
        ):
            action = action_for(session, email, result.classification, link.application_id)
            if action is not None:
                proposal = props.propose(
                    session,
                    action,
                    source="email",
                    utterance=f"{email.sender}: {email.subject} "
                    f"[{result.classification.value} @ {result.confidence:.2f}; {link.reason}]",
                )
                created.append(proposal)
        email.processed_at = utcnow()
        session.add(email)
        session.flush()
    return created
