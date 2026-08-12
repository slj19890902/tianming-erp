from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import RoleChecker, get_db, require_customer_access
from app.core.time_contract import utc_naive_to_api
from app.models.company_config import CompanyConfig
from app.models.contract_seal import (
    ContractSealAssetVersion,
    ContractSealedPdfArchive,
    ContractSealSelection,
)
from app.models.customer_contract import CustomerContract
from app.models.user import User
from app.services.audit_log import append_audit_event
from app.services.contract_pdf import ContractPdfFontError, render_sealed_contract_pdf
from app.services.contract_seal import ContractSealImageError, validate_contract_seal_png


router = APIRouter()
admin_only = RoleChecker({"admin"})
can_export_sealed = RoleChecker({"admin", "boss"})
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ContractSealSelectionPayload(BaseModel):
    expected_version: int = Field(ge=0)
    operation_key: str = Field(min_length=8, max_length=120)
    asset_version_id: int | None = Field(default=None, ge=1)
    is_enabled: bool


class ContractSealedExportPayload(BaseModel):
    expected_contract_version: int = Field(ge=1)
    expected_seal_selection_version: int = Field(ge=1)
    operation_key: str = Field(min_length=8, max_length=120)


def _user_snapshot(user: User) -> str:
    return (user.display_name or user.real_name or user.username or "").strip()[:100]


def _asset_dict(asset: ContractSealAssetVersion | None) -> dict | None:
    if asset is None:
        return None
    return {
        "id": asset.id,
        "version": asset.version,
        "original_filename": asset.original_filename,
        "sha256": asset.sha256,
        "width_px": asset.width_px,
        "height_px": asset.height_px,
        "uploaded_by": asset.uploaded_by_snapshot,
        "created_at": utc_naive_to_api(asset.created_at),
    }


def _selection_dict(
    selection: ContractSealSelection | None,
    asset: ContractSealAssetVersion | None,
) -> dict:
    return {
        "version": selection.version if selection is not None else 0,
        "is_enabled": bool(selection and selection.is_enabled),
        "asset": _asset_dict(asset),
        "updated_by": selection.updated_by_snapshot if selection else None,
        "updated_at": utc_naive_to_api(selection.updated_at) if selection else None,
    }


def _active_selection(db: Session) -> tuple[ContractSealSelection | None, ContractSealAssetVersion | None]:
    selection = db.get(ContractSealSelection, 1)
    asset = (
        db.get(ContractSealAssetVersion, selection.asset_version_id)
        if selection is not None and selection.asset_version_id is not None
        else None
    )
    return selection, asset


def _contract_or_404(db: Session, contract_id: int) -> CustomerContract:
    contract = db.scalar(
        select(CustomerContract)
        .options(selectinload(CustomerContract.items))
        .where(CustomerContract.id == contract_id)
    )
    if contract is None:
        raise HTTPException(status_code=404, detail="合同不存在")
    return contract


def _archive_filename(contract: CustomerContract) -> tuple[str, str]:
    number = _UNSAFE_FILENAME_CHARS.sub("_", contract.contract_no).strip(" ._")[:40]
    customer = _UNSAFE_FILENAME_CHARS.sub("_", contract.customer_name).strip(" ._")[:60]
    date_text = contract.contract_date.strftime("%Y%m%d")
    display = f"{date_text}_{number or contract.id}_{customer or '客户'}_盖章归档_v{contract.version}.pdf"
    return f"contract-{contract.id}-sealed-v{contract.version}.pdf", display


def _pdf_response(archive: ContractSealedPdfArchive) -> Response:
    ascii_name = f"contract-{archive.contract_id}-sealed-v{archive.contract_version}.pdf"
    encoded_name = quote(archive.download_filename, safe="")
    return Response(
        archive.pdf_content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Contract-Sealed-Archive-ID": str(archive.id),
            "X-Contract-Version": str(archive.contract_version),
            "X-Template-Version": archive.template_version,
            "X-Content-SHA256": archive.pdf_sha256,
        },
    )


@router.get("/status")
def seal_status(
    db: Session = Depends(get_db),
    _user: User = Depends(can_export_sealed),
) -> dict:
    selection, asset = _active_selection(db)
    return _selection_dict(selection, asset)


@router.post("/assets")
async def upload_seal_asset(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    if file.content_type not in {"image/png", "application/octet-stream"}:
        raise HTTPException(status_code=422, detail="只允许上传 PNG 格式的公司印章")
    content = await file.read()
    try:
        image = validate_contract_seal_png(content)
    except ContractSealImageError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    existing = db.scalar(
        select(ContractSealAssetVersion).where(
            ContractSealAssetVersion.sha256 == image.sha256
        )
    )
    if existing is not None:
        return {"asset": _asset_dict(existing), "already_exists": True}
    next_version = (db.scalar(select(func.max(ContractSealAssetVersion.version))) or 0) + 1
    filename = _UNSAFE_FILENAME_CHARS.sub("_", file.filename or "company-seal.png")[:200]
    asset = ContractSealAssetVersion(
        version=next_version,
        original_filename=filename or "company-seal.png",
        mime_type="image/png",
        png_content=image.content,
        sha256=image.sha256,
        width_px=image.width_px,
        height_px=image.height_px,
        uploaded_by=user.id,
        uploaded_by_snapshot=_user_snapshot(user),
    )
    db.add(asset)
    db.flush()
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="business",
        result="success",
        source="web",
        module_code="contracts",
        action_code="contract.seal.asset.upload",
        resource="ContractSealAssetVersion",
        entity_type="contract_seal_asset",
        entity_id=asset.id,
        object_ref=f"v{asset.version}",
        description="管理员导入公司印章版本",
        details={"version": asset.version, "sha256": asset.sha256},
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        duplicate = db.scalar(
            select(ContractSealAssetVersion).where(
                ContractSealAssetVersion.sha256 == image.sha256
            )
        )
        if duplicate is not None:
            return {"asset": _asset_dict(duplicate), "already_exists": True}
        raise HTTPException(status_code=409, detail="印章版本写入冲突，请重试") from error
    db.refresh(asset)
    return {"asset": _asset_dict(asset), "already_exists": False}


@router.put("/selection")
def update_seal_selection(
    payload: ContractSealSelectionPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(admin_only),
) -> dict:
    selection, current_asset = _active_selection(db)
    if selection is not None and selection.last_operation_key == payload.operation_key:
        requested_asset_id = payload.asset_version_id or selection.asset_version_id
        if (
            bool(selection.is_enabled) != bool(payload.is_enabled)
            or selection.asset_version_id != requested_asset_id
        ):
            raise HTTPException(status_code=409, detail="印章启用操作号已用于其他状态变更")
        return _selection_dict(selection, current_asset)
    actual_version = selection.version if selection is not None else 0
    if actual_version != payload.expected_version:
        raise HTTPException(status_code=409, detail="印章启用状态已更新，请刷新后重试")
    asset_id = payload.asset_version_id or (
        selection.asset_version_id if selection is not None else None
    )
    asset = db.get(ContractSealAssetVersion, asset_id) if asset_id is not None else None
    if payload.is_enabled and asset is None:
        raise HTTPException(status_code=409, detail="启用盖章导出前必须选择有效印章版本")
    if selection is None:
        selection = ContractSealSelection(
            id=1,
            asset_version_id=asset_id,
            is_enabled=payload.is_enabled,
            version=1,
            last_operation_key=payload.operation_key,
            updated_by=user.id,
            updated_by_snapshot=_user_snapshot(user),
        )
        db.add(selection)
    else:
        selection.asset_version_id = asset_id
        selection.is_enabled = payload.is_enabled
        selection.version += 1
        selection.last_operation_key = payload.operation_key
        selection.updated_by = user.id
        selection.updated_by_snapshot = _user_snapshot(user)
    db.flush()
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="business",
        result="success",
        source="web",
        module_code="contracts",
        action_code="contract.seal.selection.update",
        resource="ContractSealSelection",
        entity_type="contract_seal_selection",
        entity_id=1,
        object_ref=f"v{selection.version}",
        description="管理员更新公司印章启用状态",
        details={
            "selection_version": selection.version,
            "asset_version": asset.version if asset else None,
            "is_enabled": selection.is_enabled,
        },
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        replay = db.scalar(
            select(ContractSealSelection).where(
                ContractSealSelection.last_operation_key == payload.operation_key
            )
        )
        if replay is not None:
            replay_asset = db.get(ContractSealAssetVersion, replay.asset_version_id)
            return _selection_dict(replay, replay_asset)
        raise HTTPException(status_code=409, detail="印章启用状态写入冲突，请刷新后重试") from error
    db.refresh(selection)
    return _selection_dict(selection, asset)


@router.post("/contracts/{contract_id}/exports")
def export_sealed_contract(
    contract_id: int,
    payload: ContractSealedExportPayload,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(can_export_sealed),
) -> Response:
    existing = db.scalar(
        select(ContractSealedPdfArchive).where(
            ContractSealedPdfArchive.operation_key == payload.operation_key
        )
    )
    if existing is not None:
        if (
            existing.contract_id != contract_id
            or existing.contract_version != payload.expected_contract_version
            or existing.seal_selection_version != payload.expected_seal_selection_version
        ):
            raise HTTPException(status_code=409, detail="盖章导出操作号已用于其他合同版本")
        require_customer_access(existing.customer_id_snapshot, current_user=user, db=db)
        return _pdf_response(existing)
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    if contract.status not in {"confirmed", "converted"}:
        raise HTTPException(status_code=409, detail="只有已确认或已转订单合同可以导出盖章归档版")
    if contract.version != payload.expected_contract_version:
        raise HTTPException(status_code=409, detail="合同版本已更新，请刷新后重新导出")
    selection, asset = _active_selection(db)
    if (
        selection is None
        or not selection.is_enabled
        or asset is None
        or selection.version != payload.expected_seal_selection_version
    ):
        raise HTTPException(status_code=409, detail="公司印章状态已变化或未启用，请刷新后重试")
    company = db.get(CompanyConfig, 1)
    try:
        document = render_sealed_contract_pdf(contract, company, asset.png_content)
    except ContractPdfFontError as error:
        raise HTTPException(status_code=503, detail="合同 PDF 中文字体未配置或无法嵌入") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail="盖章合同生成失败，请联系管理员") from error
    _ascii_name, display_name = _archive_filename(contract)
    archive = ContractSealedPdfArchive(
        contract_id=contract.id,
        contract_version=contract.version,
        contract_status=contract.status,
        contract_no_snapshot=contract.contract_no,
        customer_id_snapshot=contract.customer_id,
        customer_name_snapshot=contract.customer_name,
        template_version=document.template_version,
        seal_asset_version_id=asset.id,
        seal_asset_version=asset.version,
        seal_selection_version=selection.version,
        seal_sha256=asset.sha256,
        pdf_content=document.content,
        pdf_sha256=document.sha256,
        download_filename=display_name,
        operation_key=payload.operation_key,
        actor_id=user.id,
        actor_username_snapshot=user.username,
        actor_role_snapshot=user.role,
    )
    db.add(archive)
    db.flush()
    append_audit_event(
        db,
        request=request,
        actor=user,
        event_category="business",
        result="success",
        source="web",
        module_code="contracts",
        action_code="contract.sealed_pdf.export",
        resource="ContractSealedPdfArchive",
        entity_type="contract_sealed_pdf_archive",
        entity_id=archive.id,
        object_ref=contract.contract_no,
        customer_id=contract.customer_id,
        customer_name=contract.customer_name,
        description="导出并永久归档盖章合同 PDF",
        details={
            "contract_version": contract.version,
            "template_version": document.template_version,
            "seal_version": asset.version,
            "pdf_sha256": document.sha256,
        },
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        replay = db.scalar(
            select(ContractSealedPdfArchive).where(
                ContractSealedPdfArchive.operation_key == payload.operation_key
            )
        )
        if replay is not None:
            return _pdf_response(replay)
        raise HTTPException(status_code=409, detail="盖章合同归档冲突，请重试") from error
    db.refresh(archive)
    return _pdf_response(archive)


@router.get("/contracts/{contract_id}/archives")
def list_sealed_archives(
    contract_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_export_sealed),
) -> dict:
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    archives = db.scalars(
        select(ContractSealedPdfArchive)
        .where(ContractSealedPdfArchive.contract_id == contract_id)
        .order_by(ContractSealedPdfArchive.created_at.desc(), ContractSealedPdfArchive.id.desc())
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "contract_version": row.contract_version,
                "contract_status": row.contract_status,
                "template_version": row.template_version,
                "seal_version": row.seal_asset_version,
                "pdf_sha256": row.pdf_sha256,
                "download_filename": row.download_filename,
                "actor_username": row.actor_username_snapshot,
                "actor_role": row.actor_role_snapshot,
                "created_at": utc_naive_to_api(row.created_at),
            }
            for row in archives
        ]
    }


@router.get("/archives/{archive_id}/pdf")
def download_sealed_archive(
    archive_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_export_sealed),
) -> Response:
    archive = db.get(ContractSealedPdfArchive, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="盖章合同归档不存在")
    require_customer_access(archive.customer_id_snapshot, current_user=user, db=db)
    return _pdf_response(archive)
