"""SQLModel table definitions.

Enum-valued columns are stored as plain strings; the enums in core.enums are
applied at the edges (repos, actions, CLI). Datetimes are naive UTC.
"""

import datetime as dt

from sqlmodel import Field, SQLModel

from jobtrack.core.clock import utcnow


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    domain: str | None = Field(default=None, index=True)
    industry: str | None = None
    size: str | None = None
    hq: str | None = None
    notes: str | None = None


class CompanyAlias(SQLModel, table=True):
    __tablename__ = "company_aliases"

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
    alias: str = Field(index=True)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"

    id: int | None = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="companies.id", index=True)
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
    # Immutable JD snapshot taken at capture time. Never overwritten.
    jd_markdown: str | None = None
    jd_snapshot_path: str | None = None
    jd_hash: str | None = None
    captured_at: dt.datetime = Field(default_factory=utcnow)


class Document(SQLModel, table=True):
    __tablename__ = "documents"

    id: int | None = Field(default=None, primary_key=True)
    type: str
    label: str = Field(index=True, unique=True)
    file_path: str
    content_hash: str = Field(index=True)
    extracted_text: str | None = None
    created_at: dt.datetime = Field(default_factory=utcnow)


class Application(SQLModel, table=True):
    __tablename__ = "applications"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    # NULL until an `applied` event lands; saved jobs are applications too.
    applied_at: dt.date | None = None
    resume_document_id: int | None = Field(default=None, foreign_key="documents.id")
    cover_letter_document_id: int | None = Field(default=None, foreign_key="documents.id")
    referral_contact_id: int | None = Field(default=None, foreign_key="contacts.id")
    source: str | None = None
    priority: int | None = None
    archived: bool = False


class ApplicationEvent(SQLModel, table=True):
    """Append-only. Status and all funnel analytics are derived from these rows."""

    __tablename__ = "application_events"

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="applications.id", index=True)
    type: str
    from_status: str | None = None
    to_status: str | None = None
    occurred_at: dt.datetime = Field(index=True)
    payload_json: str | None = None
    source: str = "manual"


class Contact(SQLModel, table=True):
    __tablename__ = "contacts"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    company_id: int | None = Field(default=None, foreign_key="companies.id")
    title: str | None = None
    email: str | None = None
    linkedin: str | None = None
    relationship: str | None = None
    notes: str | None = None


class Interaction(SQLModel, table=True):
    __tablename__ = "interactions"

    id: int | None = Field(default=None, primary_key=True)
    contact_id: int | None = Field(default=None, foreign_key="contacts.id")
    application_id: int | None = Field(default=None, foreign_key="applications.id", index=True)
    channel: str = "email"
    direction: str = "inbound"
    occurred_at: dt.datetime = Field(default_factory=utcnow)
    summary: str = ""


class Interview(SQLModel, table=True):
    __tablename__ = "interviews"

    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="applications.id", index=True)
    round: int = 1
    scheduled_at: dt.datetime = Field(index=True)
    format: str = "video"
    interviewers_json: str = "[]"
    prep_doc_path: str | None = None
    debrief_notes: str | None = None
    questions_asked_json: str = "[]"
    outcome: str | None = None


class TaskItem(SQLModel, table=True):
    __tablename__ = "tasks"

    id: int | None = Field(default=None, primary_key=True)
    application_id: int | None = Field(default=None, foreign_key="applications.id")
    due_at: dt.datetime = Field(index=True)
    kind: str = "follow_up"
    description: str = ""
    done_at: dt.datetime | None = None


class Email(SQLModel, table=True):
    __tablename__ = "emails"

    id: int | None = Field(default=None, primary_key=True)
    message_id: str = Field(index=True, unique=True)
    thread_id: str | None = Field(default=None, index=True)
    sender: str = ""
    subject: str = ""
    received_at: dt.datetime = Field(default_factory=utcnow)
    body_text: str = ""
    classification: str | None = None
    confidence: float | None = None
    linked_application_id: int | None = Field(default=None, foreign_key="applications.id")
    processed_at: dt.datetime | None = None


class Proposal(SQLModel, table=True):
    __tablename__ = "proposals"

    id: int | None = Field(default=None, primary_key=True)
    source: str = "cli"
    action_json: str = "{}"
    status: str = Field(default="pending", index=True)
    created_at: dt.datetime = Field(default_factory=utcnow)
    resolved_at: dt.datetime | None = None


class JobSkill(SQLModel, table=True):
    __tablename__ = "job_skills"

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="jobs.id", index=True)
    skill: str = Field(index=True)
    evidence: str | None = None


class Meta(SQLModel, table=True):
    """Key-value bookkeeping: embedding model/dim, IMAP UID checkpoints, etc."""

    __tablename__ = "meta"

    key: str = Field(primary_key=True)
    value: str = ""
