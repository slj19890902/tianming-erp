"""Versioned company selection with a legacy projection for existing document writers."""
import json
from types import SimpleNamespace
from fastapi import HTTPException
from sqlalchemy import select, update
from app.models.company_config import CompanyConfig
from app.models.company_profile import CompanyProfile, CompanySelection, ContractCompanySnapshot
from app.models.contract_seal import ContractSealState
from app.core.time_contract import beijing_now_naive

FIELDS = ("company_name", "short_name", "address", "phone", "fax", "tax_number",
          "bank_name", "bank_account", "contact_person", "contact_phone")


def encode(details):
    return json.dumps(details, ensure_ascii=False, sort_keys=True)


def legacy_details(db):
    row = db.get(CompanyConfig, 1)
    return {key: getattr(row, key, None) for key in FIELDS} | {"company_name": row.company_name if row else ""}


def profile(db, company_id):
    row = db.get(CompanyProfile, company_id)
    if row:
        return row
    if company_id == 1 and db.get(CompanySelection, 1) is None:
        old = db.get(ContractSealState, 1)
        return SimpleNamespace(id=1, company_name=legacy_details(db)["company_name"],
            details_json=encode(legacy_details(db)), version=0,
            seal_version=old.version if old else 0, active_seal_id=old.active_seal_id if old else None)
    raise HTTPException(404, "公司抬头不存在")


def initialize(db):
    state = db.get(CompanySelection, 1)
    if state:
        return state
    initial = profile(db, 1)
    db.add(CompanyProfile(id=1, company_name=initial.company_name, details_json=initial.details_json,
                         version=0, seal_version=initial.seal_version, active_seal_id=initial.active_seal_id))
    db.flush()
    state = CompanySelection(id=1, version=0, active_company_id=1, legacy_details_json=initial.details_json)
    db.add(state)
    db.flush()
    return state


def lock_selection(db, expected_version):
    initialize(db)
    changed = db.execute(update(CompanySelection).where(CompanySelection.id == 1,
        CompanySelection.version == expected_version).values(version=CompanySelection.version))
    if changed.rowcount != 1:
        raise HTTPException(409, "开单公司已变化，请刷新公司信息后重试")
    return db.get(CompanySelection, 1, populate_existing=True)


def lock_profile(db, company_id, version):
    initialize(db)
    changed = db.execute(update(CompanyProfile).where(CompanyProfile.id == company_id,
        CompanyProfile.version == version).values(version=CompanyProfile.version))
    if changed.rowcount != 1:
        raise HTTPException(409, "公司资料已变化，请重新读取后操作")
    return db.get(CompanyProfile, company_id, populate_existing=True)


def project_active(db, row):
    config = db.get(CompanyConfig, 1)
    if config is None:
        config = CompanyConfig(id=1)
        db.add(config)
    for key, value in json.loads(row.details_json).items():
        if key in FIELDS:
            setattr(config, key, value)
    config.updated_at = beijing_now_naive()


def describe(row):
    return dict(json.loads(row.details_json), id=row.id, version=row.version)


def company_settings(db):
    state = db.get(CompanySelection, 1)
    rows = list(db.scalars(select(CompanyProfile).order_by(CompanyProfile.id))) if state else [profile(db, 1)]
    active = state.active_company_id if state else 1
    config = db.get(CompanyConfig, 1)
    return dict(describe(next(row for row in rows if row.id == active)),
        selection_version=state.version if state else 0, profiles=[describe(row) for row in rows],
        updated_at=config.updated_at.isoformat() if config and config.updated_at else None)


def freeze_contract_company(db, contract):
    state = initialize(db)
    issuer = profile(db, state.active_company_id)
    db.add(ContractCompanySnapshot(contract_id=contract.id, company_id=issuer.id, details_json=issuer.details_json))


def contract_company(db, contract):
    snapshot = db.get(ContractCompanySnapshot, contract.id)
    if snapshot:
        return snapshot.company_id, SimpleNamespace(**json.loads(snapshot.details_json))
    # Pre-feature contracts belong to the original issuer, not whichever is active today.
    state = db.get(CompanySelection, 1)
    details = json.loads(state.legacy_details_json) if state else legacy_details(db)
    return 1, SimpleNamespace(**details)


def original_company(db):
    state = db.get(CompanySelection, 1)
    return SimpleNamespace(**(json.loads(state.legacy_details_json) if state else legacy_details(db)))
