"""The insight engine. Everything here is computed from events, never stored.

Definitions (also in README):
- Stage conversion: of applications that ever reached stage i, the fraction
  that ever reached any later stage. Median days = time from first reaching
  stage i to first reaching any later stage, for those that progressed.
- Response: the employer reacted at all — any status movement past `applied`,
  a rejection, or any inbound interaction / linked email.
- Positive response: ever progressed past `applied` (screening or beyond).
"""

from __future__ import annotations

import datetime as dt
import statistics

from pydantic import BaseModel
from sqlmodel import Session, col, select

from applyr.core.clock import utcnow
from applyr.core.enums import (
    POSITIVE_STATUSES,
    STATUS_ORDER,
    ProposalStatus,
    Status,
)
from applyr.core.events import derived_status, status_events
from applyr.core.models import (
    Application,
    Document,
    Email,
    Interaction,
    Interview,
    Job,
    JobSkill,
    Proposal,
    TaskItem,
)
from applyr.core.repos import applications as apps_repo
from applyr.core.repos import documents as documents_repo
from applyr.core.sla import days_since_activity, is_stale


class StageStat(BaseModel):
    stage: str
    reached: int
    progressed: int
    conversion: float | None
    median_days_to_next: float | None


class GroupStat(BaseModel):
    label: str
    applications: int
    responses: int
    response_rate: float
    positive: int
    positive_rate: float


class SkillStat(BaseModel):
    skill: str
    jobs: int
    pct: float
    in_resume: bool
    example_evidence: str | None = None


def _first_reached_map(session: Session, application_id: int) -> dict[Status, dt.datetime]:
    out: dict[Status, dt.datetime] = {}
    for ev in status_events(session, application_id):
        if ev.to_status is None:
            continue
        status = Status(ev.to_status)
        if status not in out:
            out[status] = ev.occurred_at
    return out


def funnel(session: Session) -> list[StageStat]:
    apps = apps_repo.non_archived(session)
    reached_maps = [
        _first_reached_map(session, app.id) for app in apps if app.id is not None
    ]
    stats: list[StageStat] = []
    ordered = sorted(STATUS_ORDER, key=lambda s: STATUS_ORDER[s])
    for stage in ordered[:-1]:  # accepted has nothing to progress to
        stage_order = STATUS_ORDER[stage]
        reached = [m for m in reached_maps if stage in m]
        durations: list[float] = []
        progressed = 0
        for m in reached:
            later = [
                ts
                for status, ts in m.items()
                if status in STATUS_ORDER and STATUS_ORDER[status] > stage_order
            ]
            if later:
                progressed += 1
                delta = min(later) - m[stage]
                durations.append(max(delta.total_seconds() / 86400.0, 0.0))
        stats.append(
            StageStat(
                stage=stage.value,
                reached=len(reached),
                progressed=progressed,
                conversion=progressed / len(reached) if reached else None,
                median_days_to_next=round(statistics.median(durations), 1)
                if durations
                else None,
            )
        )
    return stats


def response_kind(session: Session, application_id: int) -> str:
    reached = _first_reached_map(session, application_id)
    if any(status in POSITIVE_STATUSES for status in reached):
        return "positive"
    if Status.REJECTED in reached:
        return "response"
    inbound = session.exec(
        select(Interaction)
        .where(Interaction.application_id == application_id)
        .where(Interaction.direction == "inbound")
        .limit(1)
    ).first()
    if inbound is not None:
        return "response"
    linked = session.exec(
        select(Email).where(Email.linked_application_id == application_id).limit(1)
    ).first()
    if linked is not None:
        return "response"
    return "none"


def _applied_apps(session: Session) -> list[Application]:
    return [a for a in apps_repo.non_archived(session) if a.applied_at is not None]


def _group_stats(
    session: Session, groups: dict[str, list[Application]]
) -> list[GroupStat]:
    out: list[GroupStat] = []
    for label, apps in sorted(groups.items()):
        kinds = [response_kind(session, a.id) for a in apps if a.id is not None]
        responses = sum(1 for k in kinds if k != "none")
        positive = sum(1 for k in kinds if k == "positive")
        n = len(apps)
        out.append(
            GroupStat(
                label=label,
                applications=n,
                responses=responses,
                response_rate=responses / n if n else 0.0,
                positive=positive,
                positive_rate=positive / n if n else 0.0,
            )
        )
    out.sort(key=lambda g: (-g.positive_rate, -g.response_rate, g.label))
    return out


def by_resume(session: Session) -> list[GroupStat]:
    groups: dict[str, list[Application]] = {}
    for app in _applied_apps(session):
        if app.resume_document_id is not None:
            doc = session.get(Document, app.resume_document_id)
            label = doc.label if doc else f"doc#{app.resume_document_id}"
        else:
            label = "(no resume pinned)"
        groups.setdefault(label, []).append(app)
    return _group_stats(session, groups)


def by_source(session: Session) -> list[GroupStat]:
    groups: dict[str, list[Application]] = {}
    for app in _applied_apps(session):
        groups.setdefault(app.source or "(unknown)", []).append(app)
    return _group_stats(session, groups)


TIME_BUCKETS: list[tuple[str, int, int]] = [
    ("0-1d", 0, 1),
    ("2-3d", 2, 3),
    ("4-7d", 4, 7),
    ("8-14d", 8, 14),
    ("15d+", 15, 10_000),
]


def by_time_to_apply(session: Session) -> list[GroupStat]:
    groups: dict[str, list[Application]] = {}
    for app in _applied_apps(session):
        job = session.get(Job, app.job_id)
        if job is None or job.posted_at is None or app.applied_at is None:
            continue
        days = (app.applied_at - job.posted_at).days
        if days < 0:
            continue
        for label, lo, hi in TIME_BUCKETS:
            if lo <= days <= hi:
                groups.setdefault(label, []).append(app)
                break
    stats = _group_stats(session, groups)
    order = {label: i for i, (label, _, _) in enumerate(TIME_BUCKETS)}
    stats.sort(key=lambda g: order.get(g.label, 99))
    return stats


def skill_gap(session: Session) -> tuple[list[SkillStat], str | None, int]:
    """Returns (stats, resume_label, jobs_with_extraction).

    pct is relative to targeted jobs that have extracted skills — honest about
    coverage, no pretence of an 'ATS score'.
    """
    apps = apps_repo.non_archived(session)
    job_ids = {a.job_id for a in apps}
    if not job_ids:
        return [], None, 0
    rows = [
        r
        for r in session.exec(select(JobSkill)).all()
        if r.job_id in job_ids
    ]
    jobs_with_skills = {r.job_id for r in rows}
    resume = documents_repo.latest_resume(session)
    resume_text = (resume.extracted_text or "").casefold() if resume else ""

    per_skill: dict[str, set[int]] = {}
    evidence: dict[str, str] = {}
    for row in rows:
        per_skill.setdefault(row.skill, set()).add(row.job_id)
        if row.evidence and row.skill not in evidence:
            evidence[row.skill] = row.evidence
    denominator = len(jobs_with_skills)
    stats = [
        SkillStat(
            skill=skill,
            jobs=len(jobs),
            pct=len(jobs) / denominator if denominator else 0.0,
            in_resume=bool(resume_text) and skill.casefold() in resume_text,
            example_evidence=evidence.get(skill),
        )
        for skill, jobs in per_skill.items()
    ]
    stats.sort(key=lambda s: (-s.pct, s.skill))
    return stats, resume.label if resume else None, denominator


class BriefInterview(BaseModel):
    ref: str
    label: str
    round: int
    format: str
    scheduled_at: dt.datetime


class BriefTask(BaseModel):
    ref: str
    description: str
    kind: str
    due_at: dt.datetime
    app_label: str | None = None
    overdue: bool


class BriefStale(BaseModel):
    ref: str
    label: str
    status: str
    days_quiet: int


class BriefProposal(BaseModel):
    ref: str
    kind: str
    source: str
    summary: str


class Briefing(BaseModel):
    interviews_next_48h: list[BriefInterview]
    tasks_due: list[BriefTask]
    stale: list[BriefStale]
    pending_proposals: list[BriefProposal]


def briefing(session: Session, sla_days: dict[str, int]) -> Briefing:
    now = utcnow()
    horizon = now + dt.timedelta(hours=48)

    interviews: list[BriefInterview] = []
    stmt = select(Interview).where(
        Interview.scheduled_at >= now - dt.timedelta(hours=2),
        Interview.scheduled_at <= horizon,
    )
    for iv in session.exec(stmt).all():
        app = session.get(Application, iv.application_id)
        interviews.append(
            BriefInterview(
                ref=f"int#{iv.id}",
                label=apps_repo.label(session, app) if app else f"app#{iv.application_id}",
                round=iv.round,
                format=iv.format,
                scheduled_at=iv.scheduled_at,
            )
        )
    interviews.sort(key=lambda i: i.scheduled_at)

    tasks: list[BriefTask] = []
    stmt2 = select(TaskItem).where(
        col(TaskItem.done_at).is_(None), TaskItem.due_at <= horizon
    )
    for task in session.exec(stmt2).all():
        app = session.get(Application, task.application_id) if task.application_id else None
        tasks.append(
            BriefTask(
                ref=f"task#{task.id}",
                description=task.description,
                kind=task.kind,
                due_at=task.due_at,
                app_label=apps_repo.label(session, app) if app else None,
                overdue=task.due_at < now,
            )
        )
    tasks.sort(key=lambda t: t.due_at)

    stale: list[BriefStale] = []
    for app in apps_repo.non_archived(session):
        if app.id is None:
            continue
        if is_stale(session, app.id, sla_days, now=now):
            status = derived_status(session, app.id)
            quiet = days_since_activity(session, app.id, now=now) or 0
            stale.append(
                BriefStale(
                    ref=f"app#{app.id}",
                    label=apps_repo.label(session, app),
                    status=status.value if status else "?",
                    days_quiet=quiet,
                )
            )
    stale.sort(key=lambda s: -s.days_quiet)

    pending: list[BriefProposal] = []
    stmt3 = select(Proposal).where(Proposal.status == ProposalStatus.PENDING.value)
    for prop in session.exec(stmt3).all():
        import json as _json

        wrapper = _json.loads(prop.action_json)
        kind = wrapper.get("action", {}).get("kind", "?")
        utterance = wrapper.get("utterance") or ""
        pending.append(
            BriefProposal(
                ref=f"proposal#{prop.id}",
                kind=str(kind),
                source=prop.source,
                summary=str(utterance)[:80],
            )
        )
    return Briefing(
        interviews_next_48h=interviews,
        tasks_due=tasks,
        stale=stale,
        pending_proposals=pending,
    )
