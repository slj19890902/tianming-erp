"""Administrator-only private seal management and explicit stamped PDF export."""
import base64
import binascii
import hashlib
import json
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, RoleChecker, get_db, require_customer_access
from app.services import company_profiles as companies
from app.models.company_profile import CompanyProfile
from app.models.contract_seal import ContractSeal, ContractSealState, ContractSealedExport
from app.models.customer_contract import CustomerContract
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.contract_pdf import ContractPdfFontError, render_contract_pdf

router = APIRouter()
management_router = APIRouter()
company_admin = RoleChecker(["admin"])
_read = PermissionChecker("contracts.view")
_private = {"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff"}


def administrator(user=Depends(_read)):
    if not user.is_active or user.role != "admin":
        raise HTTPException(403, "仅管理员可管理电子章和导出盖章合同")
    return user


class SealUpload(BaseModel):
    expected_version: int = Field(ge=0)
    company_version: int = Field(ge=0)
    image_base64: str = Field(min_length=1, max_length=2_800_000)
    size_mm: int = Field(default=40, ge=20, le=50)


class SealDisable(BaseModel):
    expected_version: int = Field(ge=1)
    company_version: int = Field(ge=0)


class SealedExport(BaseModel):
    expected_version: int = Field(ge=1)
    seal_version: int = Field(ge=1)
    operation_key: str = Field(min_length=16, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    company_id: int | None = Field(default=None, ge=1)
    company_version: int | None = Field(default=None, ge=0)


def _image(encoded):
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 2_000_000:
            raise ValueError()
        with Image.open(BytesIO(raw)) as original:
            w, h = original.size
            if (original.format not in {"PNG", "JPEG"} or getattr(original, "n_frames", 1) != 1
                    or not (32 <= w <= 2048 and 32 <= h <= 2048) or not .5 <= w / h <= 2):
                raise ValueError()
            original.load()
            # Re-encoding discards metadata and extra payload; preserve actual pixels.
            normalized = Image.new("RGBA", (w, h))
            normalized.paste(original.convert("RGBA"))
            if not normalized.getchannel("A").getbbox():
                raise ValueError()
            output = BytesIO()
            normalized.save(output, format="PNG")
        result = output.getvalue()
        if len(result) > 2_000_000:
            raise ValueError()
        return result, w, h
    except (ValueError, binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(422, "请上传2MB以内、32至2048像素的单张PNG或JPEG章图，长宽比须在1:2至2:1之间") from None


def _state(db, company_id):
    state = companies.profile(db, company_id)
    seal = db.get(ContractSeal, state.active_seal_id) if state and state.active_seal_id else None
    return dict(version=state.seal_version, company_id=state.id, company_name=state.company_name,
                company_version=state.version, available=seal is not None,
                size_mm=seal.size_mm if seal else 40, sha256=seal.sha256 if seal else None)


def _lock_state(db, company_id, company_version, version):
    companies.lock_profile(db, company_id, company_version)
    result = db.execute(update(CompanyProfile).where(CompanyProfile.id == company_id,
        CompanyProfile.seal_version == version).values(seal_version=CompanyProfile.seal_version))
    if result.rowcount != 1:
        raise HTTPException(409, "电子章已变化，请重新读取后再操作")
    return db.get(CompanyProfile, company_id, populate_existing=True)


def _audit(db, user, action, entity_id, details, customer_id=None):
    append_audit_event(db, event_category="business", result="success", source="web",
        module_code="contracts", action_code=action, resource="contract_seal", actor=user,
        entity_type="contract_seal", entity_id=entity_id, customer_id=customer_id, details=details)


@management_router.get("/settings")
def settings(response: Response, company_id: int = Query(..., ge=1),
             db: Session = Depends(get_db), user=Depends(company_admin)):
    response.headers.update(_private)
    return _state(db, company_id)


@management_router.get("/image")
def image(version: int, company_id: int = Query(..., ge=1),
          db: Session = Depends(get_db), user=Depends(company_admin)):
    state = companies.profile(db, company_id)
    if state.seal_version != version or not state.active_seal_id:
        raise HTTPException(409, "电子章已变化或停用，请重新读取")
    seal = db.get(ContractSeal, state.active_seal_id)
    return Response(seal.image_png, media_type="image/png", headers=_private)


@management_router.post("/settings")
def upload(payload: SealUpload, company_id: int = Query(..., ge=1),
           db: Session = Depends(get_db), user=Depends(company_admin)):
    png, w, h = _image(payload.image_base64)
    try:
        with atomic_bom(db):
            state = _lock_state(db, company_id, payload.company_version, payload.expected_version)
            if not state.company_name:
                raise HTTPException(422, "请先保存公司名称，再上传该公司的电子章")
            seal = ContractSeal(image_png=png, sha256=hashlib.sha256(png).hexdigest(), width_px=w,
                                height_px=h, size_mm=payload.size_mm, actor_id=user.id)
            db.add(seal); db.flush()
            state.active_seal_id = seal.id
            state.seal_version += 1
            _audit(db, user, "contract_seal_uploaded", seal.id,
                   dict(company_id=state.id, company_name=state.company_name,
                        seal_sha256=seal.sha256, version=state.seal_version, size_mm=seal.size_mm))
            db.flush()
            result = _state(db, company_id)
        db.commit()
        return result
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "电子章已被其他管理员更新，请重新读取") from None


@management_router.post("/disable")
def disable(payload: SealDisable, company_id: int = Query(..., ge=1),
            db: Session = Depends(get_db), user=Depends(company_admin)):
    with atomic_bom(db):
        state = _lock_state(db, company_id, payload.company_version, payload.expected_version)
        previous = state.active_seal_id
        state.active_seal_id = None
        state.seal_version += 1
        _audit(db, user, "contract_seal_disabled", previous, dict(company_id=state.id, version=state.seal_version))
    db.commit()
    return _state(db, company_id)


@router.get("/{contract_id}/settings")
def contract_settings(contract_id: int, response: Response,
                      db: Session = Depends(get_db), user=Depends(administrator)):
    from app.api.contracts import _contract_or_404
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    company_id, issuer = companies.contract_company(db, contract)
    result = _state(db, company_id)
    if result["company_name"] != issuer.company_name:
        result.update(available=False, reason="公司名称已改变，与合同保存的供方不符，请新建正确抬头的合同")
    response.headers.update(_private)
    return dict(result, issuer_name=issuer.company_name)


def _download(receipt):
    return Response(receipt.pdf_content, media_type="application/pdf", headers={**_private,
        "Content-Disposition": f"attachment; filename=contract-sealed.pdf; filename*=UTF-8''{quote(receipt.filename, safe='')}",
        "X-Contract-Version": str(receipt.contract_version),
        "X-Contract-PDF-Template-Version": receipt.template_version,
        "X-Contract-PDF-SHA256": receipt.pdf_sha256,
        "X-Contract-Seal-ID": str(receipt.seal_id), "X-Contract-Seal-Receipt": str(receipt.id)})


@router.post("/{contract_id}/pdf")
def export(contract_id: int, payload: SealedExport, db: Session = Depends(get_db), user=Depends(administrator)):
    from app.api.contracts import _contract_or_404, _contract_pdf_filename
    document_request = json.dumps(dict(contract_id=contract_id, actor_id=user.id,
        **payload.model_dump(exclude_none=True)), sort_keys=True)
    try:
        with atomic_bom(db):
            contract = _contract_or_404(db, contract_id)
            require_customer_access(contract.customer_id, current_user=user, db=db)
            prior = db.scalar(select(ContractSealedExport).where(ContractSealedExport.operation_key == payload.operation_key))
            if prior:
                if prior.request_json != document_request:
                    raise HTTPException(409, "该操作标识已用于另一份盖章导出")
                return _download(prior)
            company_id, issuer = companies.contract_company(db, contract)
            if payload.company_id != company_id or payload.company_version is None:
                raise HTTPException(409, "合同供方已明确，请刷新后读取该公司的电子章")
            state = _lock_state(db, company_id, payload.company_version, payload.seal_version)
            if not issuer.company_name or state.company_name != issuer.company_name:
                raise HTTPException(409, "电子章所属公司与合同供方不符，请新建正确抬头的合同")
            if not state.active_seal_id:
                raise HTTPException(409, "请先由管理员上传并启用电子章")
            locked = db.execute(update(CustomerContract).where(CustomerContract.id == contract_id,
                CustomerContract.version == payload.expected_version).values(version=CustomerContract.version,
                    updated_at=CustomerContract.updated_at))
            if locked.rowcount != 1:
                raise HTTPException(409, "合同版本已更新，请刷新后重新导出")
            db.refresh(contract)
            seal = db.get(ContractSeal, state.active_seal_id)
            document = render_contract_pdf(contract, issuer, seal=seal)
            _, filename = _contract_pdf_filename(contract)
            filename = filename.removesuffix(".pdf") + "-已盖章.pdf"
            receipt = ContractSealedExport(operation_key=payload.operation_key, request_json=document_request,
                contract_id=contract.id, contract_version=contract.version, seal_id=seal.id, actor_id=user.id,
                pdf_content=document.content, pdf_sha256=document.sha256, template_version=document.template_version,
                font_sha256=document.font_sha256, filename=filename)
            db.add(receipt); db.flush()
            _audit(db, user, "contract_sealed_pdf_exported", seal.id, dict(receipt_id=receipt.id,
                contract_id=contract.id, contract_version=contract.version, seal_version=state.seal_version,
                company_id=state.id, company_name=issuer.company_name,
                seal_sha256=seal.sha256, pdf_sha256=document.sha256), contract.customer_id)
        response = _download(receipt)
        db.commit()
        return response
    except ContractPdfFontError:
        db.rollback()
        raise HTTPException(503, "合同 PDF 中文字体未配置或无法嵌入，请联系管理员") from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "合同或用章操作已变化，请重新读取后重试") from None
