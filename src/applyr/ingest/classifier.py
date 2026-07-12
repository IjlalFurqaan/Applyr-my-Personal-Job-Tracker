"""Local LLM email classification. Always local — mail never leaves the machine."""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from applyr.core.enums import EmailClass
from applyr.llm.jsonutil import extract_json_object
from applyr.llm.provider import ChatMessage, LLMProvider

_SYSTEM = (
    "You classify emails for a personal job-application tracker. Respond with ONLY a "
    'JSON object: {"classification": "<class>", "confidence": <0.0-1.0>}.\n'
    "Classes:\n"
    "- rejection: an explicit no for a specific application\n"
    "- interview_invite: invitation to interview or to pick an interview slot\n"
    "- assessment: take-home exercise or online test to complete\n"
    "- recruiter_outreach: a recruiter initiating contact about a role\n"
    "- offer: a job offer or offer details\n"
    "- scheduling: logistics for an already-agreed meeting (reschedules, links, rooms)\n"
    "- irrelevant: everything else — newsletters, job-alert digests, receipts, personal mail\n"
    "Confidence reflects how certain you are. Automated job-board digests are irrelevant "
    "even when they mention companies."
)


class Classification(BaseModel):
    classification: EmailClass
    confidence: float = Field(ge=0.0, le=1.0)


def classify(
    provider: LLMProvider, sender: str, subject: str, body: str
) -> Classification:
    messages = [
        ChatMessage(role="system", content=_SYSTEM),
        ChatMessage(
            role="user",
            content=f"From: {sender}\nSubject: {subject}\n\n{body[:3000]}",
        ),
    ]
    for attempt in range(2):
        text = provider.chat(messages)
        try:
            return Classification.model_validate(extract_json_object(text))
        except (ValueError, ValidationError):
            if attempt == 0:
                messages.append(ChatMessage(role="assistant", content=text))
                messages.append(
                    ChatMessage(
                        role="user",
                        content="Return ONLY the JSON object, no other text.",
                    )
                )
    return Classification(classification=EmailClass.IRRELEVANT, confidence=0.0)
