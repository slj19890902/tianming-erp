"""Administrator-managed issuer profiles; invoice seller confirmation stays independent."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.api.deps import RoleChecker, get_db
from app.models.company_profile import CompanyProfile
from app.services import company_profiles as companies
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom

router = APIRouter()
admin = RoleChecker(["admin"])


class CompanyDetails(BaseModel):
    company_name: str = Field(min_length=1, max_length=100)
    short_name: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    fax: str | None = Field(default=None, max_length=50)
    tax_number: str | None = Field(default=None, max_length=50)
    bank_name: str | None = Field(default=None, max_length=100)
    bank_account: str | None = Field(default=None, max_length=50)
    contact_person: str | None = Field(default=None, max_length=50)
    contact_phone: str | None = Field(default=None, max_length=50)

    @field_validator("*", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class CompanyCreate(CompanyDetails):
    selection_version: int = Field(ge=0)


class CompanyUpdate(CompanyDetails):
    expected_version: int = Field(ge=0)
    selection_version: int = Field(ge=0)


class CompanyActivate(BaseModel):
    expected_version: int = Field(ge=0)
    selection_version: int = Field(ge=0)


def audit(db, user, action, company_id, version):
    append_audit_event(db, event_category="business", result="success", source="web",
        module_code="system", action_code=action, resource="company_profile", actor=user,
        entity_type="company_profile", entity_id=company_id,
        details={"version": version, "company_name": companies.profile(db, company_id).company_name})


@router.get("/company")
def settings(response: Response, db: Session = Depends(get_db), user=Depends(admin)):
    response.headers["Cache-Control"] = "private, no-store"
    return companies.company_settings(db)


@router.post("/companies", status_code=201)
def create(payload: CompanyCreate, db: Session = Depends(get_db), user=Depends(admin)):
    try:
        with atomic_bom(db):
            state = companies.lock_selection(db, payload.selection_version)
            details = payload.model_dump(include=set(companies.FIELDS))
            row = CompanyProfile(company_name=payload.company_name, details_json=companies.encode(details),
                                 version=1, seal_version=0)
            db.add(row)
            db.flush()
            state.version += 1
            audit(db, user, "company_created", row.id, row.version)
            result = {"company": companies.describe(row), "settings": companies.company_settings(db)}
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "公司名称已存在或资料已变化，请刷新后重试") from None


@router.put("/companies/{company_id}")
def edit(company_id: int, payload: CompanyUpdate, db: Session = Depends(get_db), user=Depends(admin)):
    try:
        with atomic_bom(db):
            state = companies.lock_selection(db, payload.selection_version)
            row = companies.lock_profile(db, company_id, payload.expected_version)
            if row.company_name != payload.company_name:
                # A renamed legal entity must never inherit the old named seal.
                row.active_seal_id = None
                row.seal_version += 1
            row.company_name = payload.company_name
            row.details_json = companies.encode(payload.model_dump(include=set(companies.FIELDS)))
            row.version += 1
            state.version += 1
            if state.active_company_id == row.id:
                companies.project_active(db, row)
            audit(db, user, "UPDATE_COMPANY_CONFIG", row.id, row.version)
            db.flush()
            result = companies.company_settings(db)
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "公司名称已存在或资料已变化，请刷新后重试") from None


@router.post("/companies/{company_id}/activate")
def activate(company_id: int, payload: CompanyActivate, db: Session = Depends(get_db), user=Depends(admin)):
    try:
        with atomic_bom(db):
            state = companies.lock_selection(db, payload.selection_version)
            row = companies.lock_profile(db, company_id, payload.expected_version)
            if not row.company_name:
                raise HTTPException(422, "请先完善公司名称")
            state.active_company_id = row.id
            state.version += 1
            companies.project_active(db, row)
            audit(db, user, "company_activated", row.id, state.version)
            db.flush()
            result = companies.company_settings(db)
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "公司资料已变化，请刷新后重试") from None
