from __future__ import annotations

import hashlib
from pathlib import Path

from sqlmodel import Session, select

from applyr.core.enums import DocumentType
from applyr.core.models import Document


class DocumentError(Exception):
    pass


def extract_text(path: Path) -> str | None:
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return None
    if suffix in {".txt", ".md", ".markdown"}:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return None  # .docx etc. not supported; register still works, gap map won't see it


def register(session: Session, file_path: str, doc_type: DocumentType, label: str) -> Document:
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise DocumentError(f"document file not found: {path}")
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = session.exec(select(Document).where(Document.label == label)).first()
    if existing is not None:
        if existing.content_hash == content_hash:
            return existing
        raise DocumentError(
            f"label {label!r} already registered with different content; pick a new label"
        )
    doc = Document(
        type=doc_type.value,
        label=label,
        file_path=str(path),
        content_hash=content_hash,
        extracted_text=extract_text(path),
    )
    session.add(doc)
    session.flush()
    return doc


def get_by_label(session: Session, label: str) -> Document | None:
    return session.exec(select(Document).where(Document.label == label)).first()


def latest_resume(session: Session) -> Document | None:
    """The 'active' resume = most recently registered resume document."""
    stmt = select(Document).where(Document.type == DocumentType.RESUME.value)
    docs = list(session.exec(stmt).all())
    if not docs:
        return None
    return max(docs, key=lambda d: d.created_at)
