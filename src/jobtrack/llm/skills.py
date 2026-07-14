"""JD skill extraction for the gap map. Stored per job in job_skills with the
JD sentence as evidence — framed as evidence, never as an 'ATS score'."""

from __future__ import annotations

from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select

from jobtrack.core.models import Job, JobSkill
from jobtrack.core.repos import applications as apps_repo
from jobtrack.llm.jsonutil import extract_json_array
from jobtrack.llm.provider import ChatMessage, LLMProvider

_SYSTEM = (
    "Extract the skills, technologies, tools and methodologies REQUIRED or requested "
    "by this job description. Respond with ONLY a JSON array like "
    '[{"skill": "kubernetes", "evidence": "the JD sentence mentioning it"}]. '
    "Rules: skill names short, lowercase, canonical (\"kubernetes\" not \"K8s experience\"; "
    "\"python\" not \"strong Python skills\"); one entry per distinct skill; "
    "max 25 entries; no soft skills like \"communication\"."
)


class ExtractedSkill(BaseModel):
    skill: str
    evidence: str | None = None


def extract_skills(provider: LLMProvider, jd_markdown: str) -> list[ExtractedSkill]:
    messages = [
        ChatMessage(role="system", content=_SYSTEM),
        ChatMessage(role="user", content=jd_markdown[:8000]),
    ]
    for attempt in range(2):
        text = provider.chat(messages)
        try:
            raw = extract_json_array(text)
            out: list[ExtractedSkill] = []
            seen: set[str] = set()
            for item in raw:
                parsed = ExtractedSkill.model_validate(item)
                key = parsed.skill.strip().casefold()
                if key and key not in seen:
                    seen.add(key)
                    out.append(ExtractedSkill(skill=key, evidence=parsed.evidence))
            return out
        except (ValueError, ValidationError):
            if attempt == 0:
                messages.append(ChatMessage(role="assistant", content=text))
                messages.append(
                    ChatMessage(role="user", content="Return ONLY the JSON array, nothing else.")
                )
    return []


def extract_missing(session: Session, provider: LLMProvider) -> tuple[int, int]:
    """Extract skills for targeted jobs that have a JD but no skills yet.

    Returns (jobs_processed, skills_added).
    """
    targeted_job_ids = {a.job_id for a in apps_repo.non_archived(session)}
    done_job_ids = {row.job_id for row in session.exec(select(JobSkill)).all()}
    jobs_processed = skills_added = 0
    for job_id in sorted(targeted_job_ids - done_job_ids):
        job = session.get(Job, job_id)
        if job is None or not job.jd_markdown:
            continue
        for extracted in extract_skills(provider, job.jd_markdown):
            session.add(
                JobSkill(job_id=job_id, skill=extracted.skill, evidence=extracted.evidence)
            )
            skills_added += 1
        jobs_processed += 1
        session.flush()
    return jobs_processed, skills_added
