"""Enums and status ordering. DB columns store the plain string values."""

from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    SAVED = "saved"
    APPLYING = "applying"
    APPLIED = "applied"
    SCREENING = "screening"
    ASSESSMENT = "assessment"
    INTERVIEWING = "interviewing"
    FINAL_ROUND = "final_round"
    OFFER = "offer"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


TERMINAL_STATUSES: frozenset[Status] = frozenset(
    {Status.ACCEPTED, Status.REJECTED, Status.WITHDRAWN}
)

# Pipeline progression order. Terminal exits (rejected/withdrawn) sit outside it;
# accepted is the successful end of the funnel.
STATUS_ORDER: dict[Status, int] = {
    Status.SAVED: 0,
    Status.APPLYING: 1,
    Status.APPLIED: 2,
    Status.SCREENING: 3,
    Status.ASSESSMENT: 4,
    Status.INTERVIEWING: 5,
    Status.FINAL_ROUND: 6,
    Status.OFFER: 7,
    Status.ACCEPTED: 8,
}

# Statuses that count as "the employer responded positively" (past applied).
POSITIVE_STATUSES: frozenset[Status] = frozenset(
    s for s, order in STATUS_ORDER.items() if order > STATUS_ORDER[Status.APPLIED]
)


class EventType(str, Enum):
    STATUS_CHANGE = "status_change"
    NOTE = "note"


class EventSource(str, Enum):
    MANUAL = "manual"
    EMAIL = "email"
    LLM = "llm"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class DocumentType(str, Enum):
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    PORTFOLIO = "portfolio"


class EmailClass(str, Enum):
    REJECTION = "rejection"
    INTERVIEW_INVITE = "interview_invite"
    ASSESSMENT = "assessment"
    RECRUITER_OUTREACH = "recruiter_outreach"
    OFFER = "offer"
    SCHEDULING = "scheduling"
    IRRELEVANT = "irrelevant"


class InteractionChannel(str, Enum):
    EMAIL = "email"
    LINKEDIN = "linkedin"
    PHONE = "phone"
    IN_PERSON = "in_person"
    OTHER = "other"


class Direction(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class InterviewFormat(str, Enum):
    PHONE = "phone"
    VIDEO = "video"
    ONSITE = "onsite"
    TAKE_HOME = "take_home"


class InterviewOutcome(str, Enum):
    ADVANCED = "advanced"
    REJECTED = "rejected"
    PENDING = "pending"
    UNKNOWN = "unknown"


class TaskKind(str, Enum):
    FOLLOW_UP = "follow_up"
    PREP = "prep"
    THANK_YOU = "thank_you"
    DEADLINE = "deadline"
    OTHER = "other"
