"""LLM-facing Action models — the only vocabulary of writes.

Every mutation is one of these, produced by a tool handler (never by the LLM
directly), stored on a Proposal, rendered as a diff, and committed only after
human confirmation. Refs are already resolved to ids by the time an Action is
built; nested New* payloads represent entities the proposal will create.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from jobtrack.core.clock import utcnow
from jobtrack.core.enums import (
    Direction,
    DocumentType,
    InteractionChannel,
    InterviewFormat,
    InterviewOutcome,
    Status,
    TaskKind,
)

# Hard floor: only these kinds may ever bypass interactive confirmation,
# and then only if the user's config also opts in. Status changes and new
# applications always confirm.
AUTO_APPROVABLE_KINDS: frozenset[str] = frozenset({"add_note", "log_interaction"})

_MIN_DATE = dt.datetime(2020, 1, 1)


def _check_past_bounds(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(dt.UTC).replace(tzinfo=None)
    if value < _MIN_DATE:
        raise ValueError(f"timestamp {value.isoformat()} is implausibly old")
    if value > utcnow() + dt.timedelta(days=1):
        raise ValueError(f"timestamp {value.isoformat()} is in the future")
    return value


class NewCompany(BaseModel):
    name: str
    domain: str | None = None
    industry: str | None = None
    size: str | None = None
    hq: str | None = None
    notes: str | None = None


class NewDocument(BaseModel):
    file_path: str
    type: DocumentType = DocumentType.RESUME
    label: str


class AddCompany(BaseModel):
    kind: Literal["add_company"] = "add_company"
    company: NewCompany


class AddJob(BaseModel):
    kind: Literal["add_job"] = "add_job"
    company_id: int | None = None
    new_company: NewCompany | None = None
    title: str
    level: str | None = None
    location: str | None = None
    remote_policy: str | None = None
    comp_min: int | None = None
    comp_max: int | None = None
    currency: str | None = None
    source: str | None = None
    url: str | None = None
    posted_at: dt.date | None = None
    # JD capture (fetch/convert/hash) happens before the action is built, so
    # committing a proposal never touches the network.
    jd_markdown: str | None = None
    jd_snapshot_path: str | None = None
    jd_hash: str | None = None
    save: bool = True  # also create the application row at `saved`

    @model_validator(mode="after")
    def _one_company(self) -> AddJob:
        if (self.company_id is None) == (self.new_company is None):
            raise ValueError("exactly one of company_id / new_company is required")
        return self


class LogApplication(BaseModel):
    kind: Literal["log_application"] = "log_application"
    job_id: int
    applied_at: dt.date | None = None  # defaults to today at commit time
    resume_document_id: int | None = None
    new_resume: NewDocument | None = None
    cover_letter_document_id: int | None = None
    new_cover_letter: NewDocument | None = None
    source: str | None = None
    referral_contact_id: int | None = None
    priority: int | None = None


class UpdateStatus(BaseModel):
    kind: Literal["update_status"] = "update_status"
    application_id: int
    to_status: Status
    occurred_at: dt.datetime | None = None
    note: str | None = None

    @field_validator("occurred_at")
    @classmethod
    def _bounds(cls, v: dt.datetime | None) -> dt.datetime | None:
        return _check_past_bounds(v)


class LogInteraction(BaseModel):
    kind: Literal["log_interaction"] = "log_interaction"
    application_id: int | None = None
    contact_id: int | None = None
    channel: InteractionChannel = InteractionChannel.EMAIL
    direction: Direction = Direction.INBOUND
    summary: str
    occurred_at: dt.datetime | None = None

    @field_validator("occurred_at")
    @classmethod
    def _bounds(cls, v: dt.datetime | None) -> dt.datetime | None:
        return _check_past_bounds(v)

    @model_validator(mode="after")
    def _target(self) -> LogInteraction:
        if self.application_id is None and self.contact_id is None:
            raise ValueError("interaction needs an application or a contact")
        return self


class LogInterview(BaseModel):
    kind: Literal["log_interview"] = "log_interview"
    application_id: int
    scheduled_at: dt.datetime  # future allowed, obviously
    round: int | None = None  # defaults to previous round + 1 at commit
    format: InterviewFormat = InterviewFormat.VIDEO
    interviewers: list[str] = Field(default_factory=list)

    @field_validator("scheduled_at")
    @classmethod
    def _not_ancient(cls, v: dt.datetime) -> dt.datetime:
        if v.tzinfo is not None:
            v = v.astimezone(dt.UTC).replace(tzinfo=None)
        if v < _MIN_DATE:
            raise ValueError("scheduled_at is implausibly old")
        return v


class LogDebrief(BaseModel):
    kind: Literal["log_debrief"] = "log_debrief"
    interview_id: int
    notes: str
    questions_asked: list[str] = Field(default_factory=list)
    outcome: InterviewOutcome = InterviewOutcome.PENDING


class AddContact(BaseModel):
    kind: Literal["add_contact"] = "add_contact"
    name: str
    company_id: int | None = None
    new_company: NewCompany | None = None
    title: str | None = None
    email: str | None = None
    linkedin: str | None = None
    relationship: str | None = None
    notes: str | None = None


class AddNote(BaseModel):
    kind: Literal["add_note"] = "add_note"
    application_id: int
    text: str


class AddTask(BaseModel):
    kind: Literal["add_task"] = "add_task"
    application_id: int | None = None
    due_at: dt.datetime
    task_kind: TaskKind = TaskKind.FOLLOW_UP
    description: str = ""


Action = Annotated[
    AddCompany
    | AddJob
    | LogApplication
    | UpdateStatus
    | LogInteraction
    | LogInterview
    | LogDebrief
    | AddContact
    | AddNote
    | AddTask,
    Field(discriminator="kind"),
]

action_adapter: TypeAdapter[Action] = TypeAdapter(Action)


def is_auto_approvable(action_kind: str, config_auto: tuple[str, ...]) -> bool:
    return action_kind in AUTO_APPROVABLE_KINDS and action_kind in config_auto
