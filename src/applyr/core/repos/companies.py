from __future__ import annotations

from sqlmodel import Session, col, select

from applyr.core.actions import NewCompany
from applyr.core.models import Company, CompanyAlias
from applyr.core.normalize import normalize_company


def create(session: Session, data: NewCompany) -> Company:
    company = Company(
        name=data.name,
        domain=data.domain,
        industry=data.industry,
        size=data.size,
        hq=data.hq,
        notes=data.notes,
    )
    session.add(company)
    session.flush()
    return company


def add_alias(session: Session, company_id: int, alias: str) -> CompanyAlias:
    row = CompanyAlias(company_id=company_id, alias=alias)
    session.add(row)
    session.flush()
    return row


def all_match_texts(session: Session) -> list[tuple[int, str]]:
    """(company_id, normalized text) for every name and alias."""
    out: list[tuple[int, str]] = []
    for company in session.exec(select(Company)).all():
        assert company.id is not None
        out.append((company.id, normalize_company(company.name)))
    for alias in session.exec(select(CompanyAlias)).all():
        out.append((alias.company_id, normalize_company(alias.alias)))
    return out


def find_exact(session: Session, text: str) -> Company | None:
    wanted = normalize_company(text)
    for company_id, normalized in all_match_texts(session):
        if normalized == wanted:
            return session.get(Company, company_id)
    return None


def find_by_domain(session: Session, domain: str) -> Company | None:
    domain = domain.casefold().strip()
    stmt = select(Company).where(col(Company.domain).is_not(None))
    for company in session.exec(stmt).all():
        if company.domain and (
            domain == company.domain.casefold() or domain.endswith("." + company.domain.casefold())
        ):
            return company
    return None
