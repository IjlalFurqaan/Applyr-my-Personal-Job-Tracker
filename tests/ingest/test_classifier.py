from __future__ import annotations

from jobtrack.core.enums import EmailClass
from jobtrack.ingest.classifier import classify
from tests.fakes import FakeProvider


def test_valid_json_is_parsed() -> None:
    provider = FakeProvider(
        chat_responses=['{"classification": "rejection", "confidence": 0.92}']
    )
    result = classify(provider, "no-reply@stripe.com", "Your application", "Unfortunately...")
    assert result.classification is EmailClass.REJECTION
    assert result.confidence == 0.92


def test_code_fenced_json_is_parsed() -> None:
    provider = FakeProvider(
        chat_responses=['```json\n{"classification": "offer", "confidence": 0.8}\n```']
    )
    result = classify(provider, "hr@acme.com", "Offer", "We are pleased...")
    assert result.classification is EmailClass.OFFER


def test_retry_once_on_garbage_then_succeed() -> None:
    provider = FakeProvider(
        chat_responses=[
            "Sure! This looks like an interview invitation.",
            '{"classification": "interview_invite", "confidence": 0.85}',
        ]
    )
    result = classify(provider, "r@stripe.com", "Interview", "Can you meet Tuesday?")
    assert result.classification is EmailClass.INTERVIEW_INVITE
    assert len(provider.chat_log) == 2


def test_persistent_garbage_falls_back_to_irrelevant_zero() -> None:
    provider = FakeProvider(chat_responses=["nonsense", "more nonsense"])
    result = classify(provider, "x@y.com", "hi", "hello")
    assert result.classification is EmailClass.IRRELEVANT
    assert result.confidence == 0.0


def test_out_of_range_confidence_is_rejected_then_retried() -> None:
    provider = FakeProvider(
        chat_responses=[
            '{"classification": "rejection", "confidence": 42}',
            '{"classification": "rejection", "confidence": 0.9}',
        ]
    )
    result = classify(provider, "x@y.com", "re", "body")
    assert result.confidence == 0.9
