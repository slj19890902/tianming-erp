from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import PermissionChecker, get_db, require_customer_access
from app.api.orders import OrderCreate, OrderItemCreate, _create_order_impl
from app.core.time_contract import beijing_today, utc_naive_to_api, utc_now_naive
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.customer_contract import CustomerContract, CustomerContractItem
from app.models.order import Order
from app.models.product import Product
from app.models.user import User
from app.services.contract_pdf import ContractPdfFontError, render_contract_pdf


router = APIRouter()
contract_pdf_logger = logging.getLogger("erp.contract_pdf")
can_read = PermissionChecker("contracts.view")
can_edit = PermissionChecker("contracts.edit")
can_convert = PermissionChecker("contracts.convert")
can_create_order = PermissionChecker("orders.create")

MONEY = Decimal("0.01")
PRICE = Decimal("0.0001")
DELETE_CONFIRM_TEXT = "我确认删除合同"
STATUS_LABELS = {
    "draft": "草稿",
    "confirmed": "已确认",
    "converted": "已转订单",
}
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ContractItemPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    product_id: int | None = None
    product_code: str | None = Field(default=None, max_length=150)
    product_name: str = Field(min_length=1, max_length=250)
    specification: str | None = Field(default=None, max_length=250)
    box_style: str | None = Field(
        default=None,
        max_length=150,
        validation_alias=AliasChoices("box_style", "box_type"),
    )
    length_mm: Decimal | None = Field(default=None, gt=0)
    width_mm: Decimal | None = Field(default=None, gt=0)
    height_mm: Decimal | None = Field(default=None, gt=0)
    material_code: str | None = Field(
        default=None,
        max_length=100,
        validation_alias=AliasChoices("material_code", "material"),
    )
    flute_type: str | None = Field(default=None, max_length=20)
    quantity: int = Field(gt=0)
    unit_price: Decimal = Field(ge=0)
    remarks: str | None = None


class ContractContentPayload(BaseModel):
    contract_date: date = Field(default_factory=beijing_today)
    customer_po: str | None = Field(default=None, max_length=150)
    delivery_date: date | None = None
    remarks: str | None = None
    items: list[ContractItemPayload] = Field(min_length=1, max_length=100)


class ContractCreatePayload(ContractContentPayload):
    customer_id: int


class ContractUpdatePayload(ContractContentPayload):
    expected_version: int = Field(ge=1)


class ContractConfirmPayload(BaseModel):
    expected_version: int = Field(ge=1)


class ContractConvertPayload(BaseModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ContractDeletePayload(BaseModel):
    confirm_text: str = Field(min_length=1, max_length=50)


def _next_number(db: Session, contract_date: date) -> str:
    value = db.execute(
        text(
            """
            INSERT INTO contract_daily_sequences (sequence_date, last_value)
            VALUES (:sequence_date, 1)
            ON CONFLICT(sequence_date)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"sequence_date": contract_date.isoformat()},
    ).scalar_one()
    if value > 999:
        raise HTTPException(status_code=409, detail="当日合同流水号已超过 999")
    return f"CT-{contract_date:%Y%m%d}-{value:03d}"


def _clean(value: str | None) -> str | None:
    return (value or "").strip() or None


def _contract_pdf_filename(contract: CustomerContract) -> tuple[str, str]:
    customer_name = _UNSAFE_FILENAME_CHARS.sub("_", contract.customer_name).strip(
        " ._"
    )
    customer_name = customer_name[:60] or "customer"
    contract_no = _UNSAFE_FILENAME_CHARS.sub("_", contract.contract_no).strip(
        " ._"
    )
    contract_no = contract_no[:40] or f"contract-{contract.id}"
    contract_date = contract.contract_date.strftime("%Y%m%d")
    display_name = (
        f"{contract_date}_{contract_no}_{customer_name}_v{contract.version}.pdf"
    )
    ascii_name = f"contract-{contract.id}-v{contract.version}.pdf"
    return ascii_name, display_name


def _contract_pdf_request_id(request: Request) -> str:
    request_id = str(getattr(request.state, "request_id", "") or "").strip()
    request_id = re.sub(r"[^A-Za-z0-9._:-]", "", request_id)[:64]
    return request_id or uuid4().hex


def _add_working_days(start: date, working_days: int = 7) -> date:
    """Add Monday-Friday working days; statutory holidays remain manually editable."""
    result = start
    added = 0
    while added < working_days:
        result += timedelta(days=1)
        if result.weekday() < 5:
            added += 1
    return result


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    contract: CustomerContract,
    details: dict,
    description: str,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource="CustomerContract",
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type="customer_contract",
            entity_id=contract.id,
            description=description,
        )
    )


def _contract_or_404(db: Session, contract_id: int) -> CustomerContract:
    contract = db.scalar(
        select(CustomerContract)
        .options(
            selectinload(CustomerContract.items),
            selectinload(CustomerContract.converted_order),
        )
        .where(CustomerContract.id == contract_id)
    )
    if contract is None:
        raise HTTPException(status_code=404, detail="客户合同不存在")
    return contract


def _product_for_customer(
    db: Session,
    *,
    customer_id: int,
    product_id: int,
    require_active: bool,
) -> Product:
    product = db.get(Product, product_id)
    if product is None or product.customer_id != customer_id:
        raise HTTPException(status_code=400, detail="合同明细常用箱不属于当前客户")
    if require_active and (product.deleted_at is not None or not product.is_active):
        raise HTTPException(status_code=400, detail="合同明细关联的常用箱已停用或删除")
    return product


def _replace_items(
    db: Session,
    contract: CustomerContract,
    payloads: list[ContractItemPayload],
) -> None:
    had_existing_items = bool(contract.items)
    contract.items.clear()
    if had_existing_items:
        # Delete the old line numbers before inserting their replacements.
        # Otherwise SQLite can insert a new line 1 before deleting the old
        # line 1 and trip uq_customer_contract_items_line.
        db.flush()
    total = Decimal("0")
    for line_no, payload in enumerate(payloads, start=1):
        if payload.product_id is not None:
            _product_for_customer(
                db,
                customer_id=contract.customer_id,
                product_id=payload.product_id,
                require_active=False,
            )
        unit_price = Decimal(str(payload.unit_price)).quantize(PRICE, rounding=ROUND_HALF_UP)
        subtotal = (unit_price * payload.quantity).quantize(MONEY, rounding=ROUND_HALF_UP)
        contract.items.append(
            CustomerContractItem(
                line_no=line_no,
                product_id=payload.product_id,
                product_code=_clean(payload.product_code),
                product_name=payload.product_name.strip(),
                specification=_clean(payload.specification),
                box_style=_clean(payload.box_style),
                length_mm=payload.length_mm,
                width_mm=payload.width_mm,
                height_mm=payload.height_mm,
                material_code=_clean(payload.material_code),
                flute_type=_clean(payload.flute_type),
                quantity=payload.quantity,
                unit_price=unit_price,
                subtotal=subtotal,
                remarks=_clean(payload.remarks),
            )
        )
        total += subtotal
    contract.total_amount = total.quantize(MONEY, rounding=ROUND_HALF_UP)


def _apply_content(contract: CustomerContract, payload: ContractContentPayload) -> None:
    contract.contract_date = payload.contract_date
    contract.customer_po = _clean(payload.customer_po) or contract.contract_no
    contract.delivery_date = payload.delivery_date or _add_working_days(payload.contract_date)
    contract.remarks = _clean(payload.remarks)


def _item_dict(item: CustomerContractItem) -> dict:
    return {
        "id": item.id,
        "line_no": item.line_no,
        "product_id": item.product_id,
        "product_code": item.product_code,
        "product_name": item.product_name,
        "specification": item.specification,
        "box_style": item.box_style,
        "box_type": item.box_style,
        "length_mm": item.length_mm,
        "width_mm": item.width_mm,
        "height_mm": item.height_mm,
        "material_code": item.material_code,
        "material": item.material_code,
        "flute_type": item.flute_type,
        "quantity": item.quantity,
        "unit_price": item.unit_price,
        "subtotal": item.subtotal,
        "remarks": item.remarks,
    }


def _contract_dict(contract: CustomerContract) -> dict:
    converted_order_number = (
        contract.converted_order.order_number
        if contract.converted_order is not None
        else None
    )
    return {
        "id": contract.id,
        "contract_no": contract.contract_no,
        "customer_id": contract.customer_id,
        "customer_name": contract.customer_name,
        "customer_contact": contract.customer_contact,
        "customer_phone": contract.customer_phone,
        "customer_address": contract.customer_address,
        "invoice_title": contract.invoice_title,
        "tax_no": contract.tax_no,
        "bank_account": contract.bank_account,
        "payment_terms": contract.payment_terms,
        "contract_date": contract.contract_date,
        "customer_po": contract.customer_po,
        "delivery_date": contract.delivery_date,
        "remarks": contract.remarks,
        "total_amount": contract.total_amount,
        "status": contract.status,
        "status_label": STATUS_LABELS[contract.status],
        "version": contract.version,
        "created_by": contract.created_by,
        "created_at": utc_naive_to_api(contract.created_at),
        "updated_at": utc_naive_to_api(contract.updated_at) if contract.updated_at else None,
        "confirmed_by": contract.confirmed_by,
        "confirmed_at": utc_naive_to_api(contract.confirmed_at) if contract.confirmed_at else None,
        "converted_by": contract.converted_by,
        "converted_at": utc_naive_to_api(contract.converted_at) if contract.converted_at else None,
        "converted_order_id": contract.converted_order_id,
        "converted_order_number": converted_order_number,
        "order_number": converted_order_number,
        "items": [_item_dict(item) for item in contract.items],
    }


def _converted_response(contract: CustomerContract, db: Session) -> dict:
    order = db.get(Order, contract.converted_order_id) if contract.converted_order_id else None
    return {
        **_contract_dict(contract),
        "order_id": contract.converted_order_id,
        "order_number": order.order_number if order is not None else None,
        "converted_order_number": order.order_number if order is not None else None,
        "already_converted": True,
    }


@router.get("")
def list_contracts(
    customer_id: int = Query(..., gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, current_user=user, db=db)
    rows = db.scalars(
        select(CustomerContract)
        .options(
            selectinload(CustomerContract.items),
            selectinload(CustomerContract.converted_order),
        )
        .where(CustomerContract.customer_id == customer_id)
        .order_by(CustomerContract.contract_date.desc(), CustomerContract.id.desc())
    ).all()
    return {"items": [_contract_dict(row) for row in rows], "total": len(rows)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_contract(
    payload: ContractCreatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_edit),
) -> dict:
    require_customer_access(payload.customer_id, current_user=user, db=db)
    customer = db.get(Customer, payload.customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=404, detail="客户不存在或已停用")
    try:
        contract = CustomerContract(
            contract_no=_next_number(db, payload.contract_date),
            customer_id=customer.id,
            customer_name=customer.name,
            customer_contact=customer.contact_person,
            customer_phone=customer.phone,
            customer_address=customer.address,
            invoice_title=customer.invoice_title,
            tax_no=customer.tax_no,
            bank_account=customer.bank_account,
            payment_terms=customer.credit_terms or str(customer.payment_term_days or 0),
            status="draft",
            version=1,
            created_by=user.id,
        )
        _apply_content(contract, payload)
        _replace_items(db, contract, payload.items)
        db.add(contract)
        db.flush()
        _audit(
            db,
            user=user,
            action="CREATE",
            contract=contract,
            details={"contract_no": contract.contract_no, "customer_id": customer.id},
            description="创建客户合同草稿",
        )
        db.commit()
        return _contract_dict(_contract_or_404(db, contract.id))
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="合同编号或数据冲突") from error


@router.get("/{contract_id}")
def get_contract(
    contract_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    return _contract_dict(contract)


@router.put("/{contract_id}")
def update_contract(
    contract_id: int,
    payload: ContractUpdatePayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_edit),
) -> dict:
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    if contract.status != "draft":
        raise HTTPException(status_code=409, detail="已确认或已转订单的合同不能编辑")
    if contract.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="合同已被其他操作更新，请刷新后重试")
    try:
        old_version = contract.version
        _apply_content(contract, payload)
        _replace_items(db, contract, payload.items)
        contract.version += 1
        _audit(
            db,
            user=user,
            action="UPDATE",
            contract=contract,
            details={"contract_no": contract.contract_no, "from_version": old_version, "to_version": contract.version},
            description="更新客户合同草稿",
        )
        db.commit()
        return _contract_dict(_contract_or_404(db, contract.id))
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="合同数据冲突") from error


@router.delete("/{contract_id}")
def delete_contract(
    contract_id: int,
    payload: ContractDeletePayload = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(can_edit),
) -> dict:
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    if contract.status != "draft":
        raise HTTPException(status_code=409, detail="仅合同草稿可以删除")
    if payload.confirm_text != DELETE_CONFIRM_TEXT:
        raise HTTPException(status_code=400, detail="删除确认文字不正确")
    _audit(
        db,
        user=user,
        action="DELETE",
        contract=contract,
        details={"contract_no": contract.contract_no, "customer_id": contract.customer_id},
        description="删除客户合同草稿",
    )
    db.delete(contract)
    db.commit()
    return {"ok": True, "contract_id": contract_id}


@router.post("/{contract_id}/confirm")
def confirm_contract(
    contract_id: int,
    payload: ContractConfirmPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_edit),
) -> dict:
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    if contract.status != "draft":
        raise HTTPException(status_code=409, detail="当前合同不能确认")
    if contract.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="合同已被其他操作更新，请刷新后重试")
    customer = db.get(Customer, contract.customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=409, detail="客户已停用，不能确认合同")
    for item in contract.items:
        if item.product_id is None:
            raise HTTPException(status_code=400, detail=f"第 {item.line_no} 行必须绑定常用箱后才能确认")
        _product_for_customer(
            db,
            customer_id=contract.customer_id,
            product_id=item.product_id,
            require_active=True,
        )
    contract.status = "confirmed"
    contract.confirmed_by = user.id
    contract.confirmed_at = utc_now_naive()
    contract.version += 1
    _audit(
        db,
        user=user,
        action="CONFIRM",
        contract=contract,
        details={"contract_no": contract.contract_no, "version": contract.version},
        description="确认客户合同并锁定内容",
    )
    db.commit()
    return _contract_dict(_contract_or_404(db, contract.id))


@router.post("/{contract_id}/convert-order")
def convert_contract_to_order(
    contract_id: int,
    payload: ContractConvertPayload,
    db: Session = Depends(get_db),
    user: User = Depends(can_convert),
    _order_creator: User = Depends(can_create_order),
) -> dict:
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    if contract.status == "converted":
        return _converted_response(contract, db)
    if contract.status != "confirmed":
        raise HTTPException(status_code=409, detail="只有已确认合同可以转订单")
    if contract.version != payload.expected_version:
        raise HTTPException(status_code=409, detail="合同已被其他操作更新，请刷新后重试")
    if contract.conversion_idempotency_key and contract.conversion_idempotency_key != payload.idempotency_key:
        raise HTTPException(status_code=409, detail="合同转订单请求标识冲突")
    try:
        order_payload = OrderCreate(
            customer_id=contract.customer_id,
            customer_po=contract.customer_po,
            order_date=contract.contract_date,
            delivery_date=contract.delivery_date,
            remark=contract.remarks,
            items=[
                OrderItemCreate(
                    product_id=item.product_id,
                    product_code=item.product_code,
                    product_name=item.product_name,
                    specification=item.specification,
                    material=item.material_code,
                    flute_type=item.flute_type,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    is_new_product=False,
                )
                for item in contract.items
            ],
        )
        order_data = _create_order_impl(
            order_payload,
            db,
            user,
            commit=False,
            source_contract_id=contract.id,
        )
        order_id = int(order_data["id"])
        contract.status = "converted"
        contract.converted_order_id = order_id
        contract.conversion_idempotency_key = payload.idempotency_key
        contract.converted_by = user.id
        contract.converted_at = utc_now_naive()
        contract.version += 1
        _audit(
            db,
            user=user,
            action="CONVERT",
            contract=contract,
            details={
                "contract_no": contract.contract_no,
                "order_id": order_id,
                "order_number": order_data.get("order_number"),
                "idempotency_key": payload.idempotency_key,
            },
            description="客户合同转为销售订单",
        )
        db.commit()
        converted = _contract_or_404(db, contract.id)
        return {
            **_contract_dict(converted),
            "order_id": order_id,
            "order_number": order_data.get("order_number"),
            "converted_order_number": order_data.get("order_number"),
            "already_converted": False,
        }
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        existing = db.scalar(
            select(CustomerContract)
            .options(selectinload(CustomerContract.items))
            .where(CustomerContract.id == contract_id)
        )
        if existing is not None and existing.converted_order_id is not None:
            return _converted_response(existing, db)
        raise HTTPException(status_code=409, detail="合同转订单数据冲突") from error


@router.get("/{contract_id}/print")
def contract_print(
    contract_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return only persisted contract snapshots for the future browser print page."""
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    company = db.get(CompanyConfig, 1)
    return {
        "contract": _contract_dict(contract),
        "sender": {
            "company_name": company.company_name if company else "",
            "address": company.address if company else None,
            "phone": company.phone if company else None,
            "contact_person": company.contact_person if company else None,
        },
    }


@router.get("/{contract_id}/pdf")
def contract_pdf(
    contract_id: int,
    request: Request,
    expected_version: int = Query(..., ge=1),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> Response:
    """Download an unsealed PDF without changing any contract or workflow fact."""

    request_id = _contract_pdf_request_id(request)
    contract = _contract_or_404(db, contract_id)
    require_customer_access(contract.customer_id, current_user=user, db=db)
    if contract.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail="合同版本已更新，请刷新后重新导出 PDF",
        )
    company = db.get(CompanyConfig, 1)
    try:
        document = render_contract_pdf(contract, company)
    except ContractPdfFontError as error:
        raise HTTPException(
            status_code=503,
            detail="合同 PDF 中文字体未配置或无法嵌入，请联系管理员",
            headers={"X-Request-ID": request_id},
        ) from error
    except Exception as error:
        contract_pdf_logger.error(
            "contract_pdf_render_failed contract_id=%s contract_version=%s "
            "actor_id=%s request_id=%s error_type=%s",
            contract.id,
            contract.version,
            user.id,
            request_id,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": "合同 PDF 生成失败：服务器内部错误",
                "code": "CONTRACT_PDF_RENDER_FAILED",
                "request_id": request_id,
            },
            headers={"X-Request-ID": request_id},
        ) from error
    ascii_name, display_name = _contract_pdf_filename(contract)
    encoded_name = quote(display_name, safe="")
    return Response(
        content=document.content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{encoded_name}"
            ),
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Content-Type-Options": "nosniff",
            "X-Contract-Version": str(contract.version),
            "X-Contract-PDF-Template-Version": document.template_version,
            "X-Contract-PDF-SHA256": document.sha256,
            "X-Request-ID": request_id,
            "ETag": f'"sha256-{document.sha256}"',
        },
    )
