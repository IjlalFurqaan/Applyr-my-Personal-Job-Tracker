"""Deterministic entity resolution. No LLM calls in this module, by design.

The model supplies strings ("Stripe", "the Stripe backend role"); this code
decides what they refer to, or refuses with candidates. It never guesses
between two plausible targets.

Thresholds: a single fuzzy match >= 92 resolves; anything in 70..91 (or
multiple >= 92) disambiguates; below 70 is not found.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz
from sqlmodel import Session, select

from applyr.core.clock import utcnow
from applyr.core.enums import TERMINAL_STATUSES
from applyr.core.events import derived_status, last_activity
from applyr.core.models import (
    Application,
    Company,
    CompanyAlias,
    Contact,
    Document,
    Interview,
    Job,
)
from applyr.core.normalize import content_tokens, normalize_company, tokens
from applyr.core.repos import applications as apps_repo
from applyr.core.repos import companies as companies_repo

AUTO_THRESHOLD = 92.0
CANDIDATE_THRESHOLD = 70.0

_REF_RE = re.compile(r"^\s*(app|job|co|contact|int|doc)#(\d+)\s*$", re.IGNORECASE)


@dataclass
class Candidate:
    ref: str
    label: str
    status: str | None = None
    last_activity: str | None = None


@dataclass
class Resolution:
    outcome: Literal["resolved", "ambiguous", "not_found"]
    entity_id: int | None = None
    candidates: list[Candidate] = field(default_factory=list)
    note: str | None = None  # assumption made while resolving, surfaced in the diff
    hint: str | None = None  # human-readable guidance for ambiguous/not_found


def parse_ref(text: str) -> tuple[str, int] | None:
    m = _REF_RE.match(text)
    if m is None:
        return None
    return m.group(1).casefold(), int(m.group(2))


def _company_candidate(session: Session, company_id: int) -> Candidate:
    company = session.get(Company, company_id)
    return Candidate(
        ref=f"co#{company_id}",
        label=company.name if company else f"co#{company_id}",
    )


def _app_candidate(session: Session, app: Application) -> Candidate:
    assert app.id is not None
    status = derived_status(session, app.id)
    activity = last_activity(session, app.id)
    return Candidate(
        ref=f"app#{app.id}",
        label=apps_repo.label(session, app),
        status=status.value if status else None,
        last_activity=activity.date().isoformat() if activity else None,
    )


def resolve_company(session: Session, text: str) -> Resolution:
    ref = parse_ref(text)
    if ref is not None:
        kind, entity_id = ref
        if kind != "co":
            return Resolution("not_found", hint=f"{text.strip()} is not a company ref")
        company = session.get(Company, entity_id)
        if company is None:
            return Resolution("not_found", hint=f"co#{entity_id} does not exist")
        return Resolution("resolved", entity_id=entity_id)

    wanted = normalize_company(text)
    if not wanted:
        return Resolution("not_found", hint="empty company reference")

    best: dict[int, float] = {}
    for company_id, normalized in companies_repo.all_match_texts(session):
        score = float(fuzz.token_set_ratio(wanted, normalized))
        if score > best.get(company_id, 0.0):
            best[company_id] = score

    high = [cid for cid, score in best.items() if score >= AUTO_THRESHOLD]
    if len(high) == 1:
        return Resolution("resolved", entity_id=high[0])
    mid = sorted(
        (cid for cid, score in best.items() if score >= CANDIDATE_THRESHOLD),
        key=lambda cid: -best[cid],
    )
    if mid:
        return Resolution(
            "ambiguous",
            candidates=[_company_candidate(session, cid) for cid in mid],
            hint=f"multiple companies match {text.strip()!r}",
        )
    return Resolution("not_found", hint=f"no company matching {text.strip()!r}")


def _company_token_set(session: Session, company_id: int) -> set[str]:
    out: set[str] = set()
    company = session.get(Company, company_id)
    if company is not None:
        out.update(tokens(company.name))
    stmt = select(CompanyAlias).where(CompanyAlias.company_id == company_id)
    for alias in session.exec(stmt).all():
        out.update(tokens(alias.alias))
    return out


def _narrow_by_tokens(
    session: Session, items: list[tuple[Application | Job, str]], leftover: list[str]
) -> list[tuple[Application | Job, str]]:
    """Keep the items whose descriptive text matches the most leftover tokens."""
    if not leftover:
        return items
    scored: list[tuple[Application | Job, str, int]] = []
    for item, description in items:
        description_tokens = set(tokens(description))
        matched = sum(1 for t in leftover if t in description_tokens)
        scored.append((item, description, matched))
    best = max(m for _, _, m in scored)
    if best == 0:
        return items
    return [(item, desc) for item, desc, m in scored if m == best]


def _job_description(session: Session, job: Job) -> str:
    return " ".join(p for p in [job.title, job.level, job.location] if p)


def resolve_application(session: Session, text: str) -> Resolution:
    ref = parse_ref(text)
    if ref is not None:
        kind, entity_id = ref
        if kind != "app":
            return Resolution("not_found", hint=f"{text.strip()} is not an application ref")
        app = session.get(Application, entity_id)
        if app is None:
            return Resolution("not_found", hint=f"app#{entity_id} does not exist")
        return Resolution("resolved", entity_id=entity_id)

    company_res = resolve_company(session, text)
    if company_res.outcome == "ambiguous":
        return Resolution(
            "ambiguous",
            candidates=company_res.candidates,
            hint=(company_res.hint or "") + " — which company did you mean?",
        )
    if company_res.outcome == "not_found":
        return Resolution("not_found", hint=company_res.hint)
    assert company_res.entity_id is not None
    company_id = company_res.entity_id

    apps = apps_repo.for_company(session, company_id)
    if not apps:
        company = session.get(Company, company_id)
        name = company.name if company else f"co#{company_id}"
        archived = apps_repo.for_company(session, company_id, include_archived=True)
        hint = f"no applications at {name}"
        if archived:
            hint += f" ({len(archived)} archived — pass an explicit app#id to target one)"
        return Resolution("not_found", hint=hint)

    company_tokens = _company_token_set(session, company_id)
    leftover = [t for t in content_tokens(text) if t not in company_tokens]
    items: list[tuple[Application | Job, str]] = []
    for app in apps:
        job = session.get(Job, app.job_id)
        items.append((app, _job_description(session, job) if job else ""))
    narrowed = [item for item, _ in _narrow_by_tokens(session, items, leftover)]

    if len(narrowed) == 1:
        chosen = narrowed[0]
        assert isinstance(chosen, Application) and chosen.id is not None
        return Resolution("resolved", entity_id=chosen.id)

    non_terminal: list[Application] = []
    for item in narrowed:
        assert isinstance(item, Application) and item.id is not None
        status = derived_status(session, item.id)
        if status is None or status not in TERMINAL_STATUSES:
            non_terminal.append(item)
    if len(non_terminal) == 1:
        app = non_terminal[0]
        assert app.id is not None
        return Resolution(
            "resolved",
            entity_id=app.id,
            note="assumed the only active application; the others are closed",
        )

    candidates = [
        _app_candidate(session, item)
        for item in narrowed
        if isinstance(item, Application)
    ]
    candidates.sort(key=lambda c: c.last_activity or "", reverse=True)
    return Resolution(
        "ambiguous",
        candidates=candidates,
        hint=f"multiple applications match {text.strip()!r} — pick one",
    )


def resolve_job(session: Session, text: str) -> Resolution:
    ref = parse_ref(text)
    if ref is not None:
        kind, entity_id = ref
        if kind != "job":
            return Resolution("not_found", hint=f"{text.strip()} is not a job ref")
        job = session.get(Job, entity_id)
        if job is None:
            return Resolution("not_found", hint=f"job#{entity_id} does not exist")
        return Resolution("resolved", entity_id=entity_id)

    company_res = resolve_company(session, text)
    if company_res.outcome != "resolved":
        return Resolution(
            company_res.outcome, candidates=company_res.candidates, hint=company_res.hint
        )
    assert company_res.entity_id is not None
    company_id = company_res.entity_id

    jobs = list(session.exec(select(Job).where(Job.company_id == company_id)).all())
    if not jobs:
        company = session.get(Company, company_id)
        return Resolution(
            "not_found",
            hint=f"no jobs captured at {company.name if company else company_id}",
        )

    company_tokens = _company_token_set(session, company_id)
    leftover = [t for t in content_tokens(text) if t not in company_tokens]
    items: list[tuple[Application | Job, str]] = [
        (job, _job_description(session, job)) for job in jobs
    ]
    narrowed = [item for item, _ in _narrow_by_tokens(session, items, leftover)]

    if len(narrowed) == 1:
        chosen = narrowed[0]
        assert isinstance(chosen, Job) and chosen.id is not None
        return Resolution("resolved", entity_id=chosen.id)

    # Prefer jobs not yet applied to (that's almost always what `apply` means).
    unapplied: list[Job] = []
    for item in narrowed:
        assert isinstance(item, Job) and item.id is not None
        apps = apps_repo.for_job(session, item.id)
        if all(a.applied_at is None for a in apps):
            unapplied.append(item)
    if len(unapplied) == 1:
        job = unapplied[0]
        assert job.id is not None
        return Resolution(
            "resolved", entity_id=job.id, note="assumed the job you haven't applied to yet"
        )

    candidates = []
    for item in narrowed:
        assert isinstance(item, Job) and item.id is not None
        company = session.get(Company, item.company_id)
        candidates.append(
            Candidate(
                ref=f"job#{item.id}",
                label=f"{company.name if company else '?'} — {item.title}",
            )
        )
    return Resolution(
        "ambiguous", candidates=candidates, hint=f"multiple jobs match {text.strip()!r}"
    )


def resolve_contact(session: Session, text: str) -> Resolution:
    ref = parse_ref(text)
    if ref is not None:
        kind, entity_id = ref
        if kind != "contact":
            return Resolution("not_found", hint=f"{text.strip()} is not a contact ref")
        contact = session.get(Contact, entity_id)
        if contact is None:
            return Resolution("not_found", hint=f"contact#{entity_id} does not exist")
        return Resolution("resolved", entity_id=entity_id)

    q = " ".join(tokens(text))
    if not q:
        return Resolution("not_found", hint="empty contact reference")
    best: dict[int, float] = {}
    labels: dict[int, str] = {}
    for contact in session.exec(select(Contact)).all():
        assert contact.id is not None
        score = float(fuzz.token_set_ratio(q, " ".join(tokens(contact.name))))
        best[contact.id] = score
        labels[contact.id] = contact.name
    high = [cid for cid, score in best.items() if score >= AUTO_THRESHOLD]
    if len(high) == 1:
        return Resolution("resolved", entity_id=high[0])
    mid = sorted(
        (cid for cid, score in best.items() if score >= CANDIDATE_THRESHOLD),
        key=lambda cid: -best[cid],
    )
    if mid:
        return Resolution(
            "ambiguous",
            candidates=[Candidate(ref=f"contact#{cid}", label=labels[cid]) for cid in mid],
            hint=f"multiple contacts match {text.strip()!r}",
        )
    return Resolution("not_found", hint=f"no contact matching {text.strip()!r}")


def resolve_interview(session: Session, text: str) -> Resolution:
    """int#id, or an application reference — picks that application's most
    recent past interview (or the next upcoming one if none are past)."""
    ref = parse_ref(text)
    if ref is not None:
        kind, entity_id = ref
        if kind == "int":
            interview = session.get(Interview, entity_id)
            if interview is None:
                return Resolution("not_found", hint=f"int#{entity_id} does not exist")
            return Resolution("resolved", entity_id=entity_id)

    app_res = resolve_application(session, text)
    if app_res.outcome != "resolved":
        return Resolution(app_res.outcome, candidates=app_res.candidates, hint=app_res.hint)
    assert app_res.entity_id is not None
    stmt = select(Interview).where(Interview.application_id == app_res.entity_id)
    interviews = sorted(session.exec(stmt).all(), key=lambda iv: iv.scheduled_at)
    if not interviews:
        return Resolution("not_found", hint="no interviews logged for that application")
    now = utcnow()
    past = [iv for iv in interviews if iv.scheduled_at <= now]
    chosen = past[-1] if past else interviews[0]
    assert chosen.id is not None
    note = None
    if len(interviews) > 1:
        note = f"assumed round {chosen.round} ({chosen.scheduled_at.date().isoformat()})"
    return Resolution("resolved", entity_id=chosen.id, note=note)


def resolve_document(session: Session, text: str) -> Resolution:
    ref = parse_ref(text)
    if ref is not None:
        kind, entity_id = ref
        if kind != "doc":
            return Resolution("not_found", hint=f"{text.strip()} is not a document ref")
        doc = session.get(Document, entity_id)
        if doc is None:
            return Resolution("not_found", hint=f"doc#{entity_id} does not exist")
        return Resolution("resolved", entity_id=entity_id)

    q = text.strip().casefold()
    docs = list(session.exec(select(Document)).all())
    exact = [d for d in docs if d.label.casefold() == q]
    if len(exact) == 1:
        assert exact[0].id is not None
        return Resolution("resolved", entity_id=exact[0].id)
    scored = [
        (d, float(fuzz.WRatio(q, d.label.casefold()))) for d in docs
    ]
    high = [d for d, s in scored if s >= AUTO_THRESHOLD]
    if len(high) == 1:
        assert high[0].id is not None
        return Resolution("resolved", entity_id=high[0].id)
    mid = [d for d, s in scored if s >= CANDIDATE_THRESHOLD]
    if mid:
        return Resolution(
            "ambiguous",
            candidates=[
                Candidate(ref=f"doc#{d.id}", label=f"{d.label} ({d.type})") for d in mid
            ],
            hint=f"multiple documents match {text.strip()!r}",
        )
    return Resolution("not_found", hint=f"no document matching {text.strip()!r}")
