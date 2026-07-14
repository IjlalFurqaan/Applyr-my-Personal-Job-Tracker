"""Search: fuzzy (always available) + semantic via sqlite-vec (optional).

The semantic index is rebuilt explicitly with `jobtrack reindex` — no magic
background embedding. If sqlite-vec or Ollama is unavailable, fuzzy search
still works and semantic search reports why it can't.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable

from pydantic import BaseModel
from rapidfuzz import fuzz
from sqlalchemy import text as sa_text
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from jobtrack.core.enums import EventType
from jobtrack.core.events import derived_status
from jobtrack.core.models import (
    ApplicationEvent,
    Company,
    CompanyAlias,
    Contact,
    Email,
    Interview,
    Job,
    Meta,
)
from jobtrack.core.repos import applications as apps_repo

EmbedFn = Callable[[list[str]], list[list[float]]]

FUZZY_MIN_SCORE = 55.0


class SearchHit(BaseModel):
    ref: str
    type: str
    headline: str
    snippet: str = ""
    score: float


class SemanticUnavailable(Exception):
    pass


def fuzzy_search(
    session: Session, query: str, *, scope: str = "all", limit: int = 10
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    q = query.casefold()

    if scope in ("all", "companies", "jobs", "applications"):
        alias_texts: dict[int, list[str]] = {}
        for alias in session.exec(select(CompanyAlias)).all():
            alias_texts.setdefault(alias.company_id, []).append(alias.alias)
        for company in session.exec(select(Company)).all():
            assert company.id is not None
            texts = [company.name, *alias_texts.get(company.id, [])]
            score = max(fuzz.WRatio(q, t.casefold()) for t in texts)
            if score >= FUZZY_MIN_SCORE and scope in ("all", "companies"):
                hits.append(
                    SearchHit(
                        ref=f"co#{company.id}",
                        type="company",
                        headline=company.name,
                        score=score,
                    )
                )

    if scope in ("all", "jobs", "applications"):
        for job in session.exec(select(Job)).all():
            assert job.id is not None
            job_company = session.get(Company, job.company_id)
            headline = f"{job_company.name if job_company else '?'} — {job.title}"
            score = fuzz.WRatio(q, headline.casefold())
            if score < FUZZY_MIN_SCORE:
                continue
            apps = apps_repo.for_job(session, job.id)
            if apps and scope in ("all", "applications"):
                for app in apps:
                    assert app.id is not None
                    status = derived_status(session, app.id)
                    hits.append(
                        SearchHit(
                            ref=f"app#{app.id}",
                            type="application",
                            headline=headline,
                            snippet=f"status: {status.value}" if status else "",
                            score=score,
                        )
                    )
            elif scope in ("all", "jobs"):
                hits.append(
                    SearchHit(ref=f"job#{job.id}", type="job", headline=headline, score=score)
                )

    if scope in ("all", "contacts"):
        for contact in session.exec(select(Contact)).all():
            assert contact.id is not None
            score = fuzz.WRatio(q, contact.name.casefold())
            if score >= FUZZY_MIN_SCORE:
                contact_company = (
                    session.get(Company, contact.company_id) if contact.company_id else None
                )
                hits.append(
                    SearchHit(
                        ref=f"contact#{contact.id}",
                        type="contact",
                        headline=contact.name,
                        snippet=contact_company.name if contact_company else "",
                        score=score,
                    )
                )

    if scope in ("all", "emails"):
        for email in session.exec(select(Email)).all():
            assert email.id is not None
            score = max(
                fuzz.partial_ratio(q, email.subject.casefold()),
                fuzz.partial_ratio(q, email.sender.casefold()),
            )
            if score >= max(FUZZY_MIN_SCORE, 70):
                hits.append(
                    SearchHit(
                        ref=f"email#{email.id}",
                        type="email",
                        headline=email.subject,
                        snippet=email.sender,
                        score=float(score) - 5,  # never outrank entity hits
                    )
                )

    if scope in ("all", "notes"):
        stmt = select(ApplicationEvent).where(
            ApplicationEvent.type == EventType.NOTE.value,
            col(ApplicationEvent.payload_json).is_not(None),
        )
        for ev in session.exec(stmt).all():
            note = json.loads(ev.payload_json or "{}").get("note", "")
            if not note:
                continue
            score = fuzz.partial_ratio(q, str(note).casefold())
            if score >= max(FUZZY_MIN_SCORE, 70):
                hits.append(
                    SearchHit(
                        ref=f"app#{ev.application_id}",
                        type="note",
                        headline=str(note)[:120],
                        score=float(score) - 5,
                    )
                )

    hits.sort(key=lambda h: h.score, reverse=True)
    deduped: list[SearchHit] = []
    seen: set[str] = set()
    for hit in hits:
        key = f"{hit.type}:{hit.ref}"
        if key not in seen:
            seen.add(key)
            deduped.append(hit)
    return deduped[:limit]


# --- semantic index ---------------------------------------------------------


def _serialize(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _chunks(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out: list[str] = []
    start = 0
    while start < len(text):
        out.append(text[start : start + size])
        start += size - overlap
    return out


def _collect_items(session: Session) -> list[tuple[str, int, str]]:
    """(item_type, item_id, text) for everything worth embedding."""
    items: list[tuple[str, int, str]] = []
    for job in session.exec(select(Job)).all():
        assert job.id is not None
        if job.jd_markdown:
            company = session.get(Company, job.company_id)
            prefix = f"{company.name if company else ''} {job.title}\n"
            for chunk in _chunks(job.jd_markdown):
                items.append(("job", job.id, prefix + chunk))
    stmt = select(ApplicationEvent).where(
        ApplicationEvent.type == EventType.NOTE.value,
        col(ApplicationEvent.payload_json).is_not(None),
    )
    for ev in session.exec(stmt).all():
        note = json.loads(ev.payload_json or "{}").get("note", "")
        if note and ev.id is not None:
            items.append(("note", ev.id, str(note)))
    for interview in session.exec(select(Interview)).all():
        assert interview.id is not None
        if interview.debrief_notes:
            items.append(("debrief", interview.id, interview.debrief_notes))
    for email in session.exec(select(Email)).all():
        assert email.id is not None
        if email.classification and email.classification != "irrelevant":
            items.append(("email", email.id, f"{email.subject}\n{email.body_text[:1500]}"))
    return items


def reindex(engine: Engine, session: Session, embed: EmbedFn, model_name: str) -> int:
    items = _collect_items(session)
    if not items:
        return 0
    vectors: list[list[float]] = []
    batch = 32
    for i in range(0, len(items), batch):
        vectors.extend(embed([text for _, _, text in items[i : i + batch]]))
    dim = len(vectors[0])
    with engine.connect() as conn:
        conn.execute(sa_text("DROP TABLE IF EXISTS vec_items"))
        conn.execute(sa_text("DROP TABLE IF EXISTS vec_map"))
        conn.execute(
            sa_text(f"CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[{dim}])")
        )
        conn.execute(
            sa_text(
                "CREATE TABLE vec_map ("
                "rowid INTEGER PRIMARY KEY, item_type TEXT, item_id INTEGER, snippet TEXT)"
            )
        )
        for rowid, ((item_type, item_id, text), vector) in enumerate(
            zip(items, vectors, strict=True), start=1
        ):
            conn.execute(
                sa_text("INSERT INTO vec_items(rowid, embedding) VALUES (:r, :e)"),
                {"r": rowid, "e": _serialize(vector)},
            )
            conn.execute(
                sa_text(
                    "INSERT INTO vec_map(rowid, item_type, item_id, snippet) "
                    "VALUES (:r, :t, :i, :s)"
                ),
                {"r": rowid, "t": item_type, "i": item_id, "s": text[:200]},
            )
        conn.commit()
    _set_meta(session, "embed_model", model_name)
    _set_meta(session, "embed_dim", str(dim))
    return len(items)


def _set_meta(session: Session, key: str, value: str) -> None:
    row = session.get(Meta, key)
    if row is None:
        row = Meta(key=key, value=value)
    else:
        row.value = value
    session.add(row)
    session.flush()


def semantic_search(
    engine: Engine,
    session: Session,
    embed: EmbedFn,
    query: str,
    *,
    expected_model: str,
    limit: int = 10,
) -> list[SearchHit]:
    indexed_model = session.get(Meta, "embed_model")
    if indexed_model is None:
        raise SemanticUnavailable("no semantic index yet — run `jobtrack reindex`")
    if indexed_model.value != expected_model:
        raise SemanticUnavailable(
            f"index was built with {indexed_model.value!r}, config says "
            f"{expected_model!r} — run `jobtrack reindex`"
        )
    vector = embed([query])[0]
    with engine.connect() as conn:
        rows = conn.execute(
            sa_text(
                "SELECT v.distance, m.item_type, m.item_id, m.snippet "
                "FROM vec_items v JOIN vec_map m ON m.rowid = v.rowid "
                "WHERE v.embedding MATCH :q AND k = :k ORDER BY v.distance"
            ),
            {"q": _serialize(vector), "k": limit},
        ).fetchall()
    hits: list[SearchHit] = []
    for distance, item_type, item_id, snippet in rows:
        ref, headline = _describe_item(session, str(item_type), int(item_id))
        hits.append(
            SearchHit(
                ref=ref,
                type=str(item_type),
                headline=headline,
                snippet=str(snippet or ""),
                score=1.0 / (1.0 + float(distance)) * 100,
            )
        )
    return hits


def _describe_item(session: Session, item_type: str, item_id: int) -> tuple[str, str]:
    if item_type == "job":
        job = session.get(Job, item_id)
        if job is not None:
            company = session.get(Company, job.company_id)
            apps = apps_repo.for_job(session, item_id)
            ref = f"app#{apps[0].id}" if apps else f"job#{item_id}"
            return ref, f"{company.name if company else '?'} — {job.title}"
    elif item_type == "note":
        ev = session.get(ApplicationEvent, item_id)
        if ev is not None:
            return f"app#{ev.application_id}", "note"
    elif item_type == "debrief":
        interview = session.get(Interview, item_id)
        if interview is not None:
            return f"int#{item_id}", f"debrief round {interview.round}"
    elif item_type == "email":
        email = session.get(Email, item_id)
        if email is not None:
            return f"email#{item_id}", email.subject
    return f"{item_type}#{item_id}", item_type
