from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from typing import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.time_contract import (
    beijing_now_naive,
    utc_naive_to_api,
    utc_now_naive,
)
from app.models.audit import OperationLog
from app.models.inventory_onboarding import (
    InventoryOnboardingBatch,
    InventoryOnboardingLine,
)
from app.models.inventory_onboarding_posting import InventoryOnboardingPosting
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLocationMovement,
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
)
from app.services import inventory_onboarding
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    manual_finished_in,
    manual_semi_finished_in,
)


MAX_POSTING_LINES = 100
MOVEMENT_REASON = "首次库存盘点正式入账"


class InventoryOnboardingPostingError(ValueError):
    def __init__(
        self,
        message: str,
        status_code: int = 409,
        code: str = "INVENTORY_ONBOARDING_POSTING_INVALID",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _error(
    message: str,
    *,
    status_code: int = 409,
    code: str,
) -> InventoryOnboardingPostingError:
    return InventoryOnboardingPostingError(message, status_code, code)


def _json_hash(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _formal_counts(db: Session) -> dict[str, int]:
    return {
        model.__tablename__: int(
            db.scalar(select(func.count(model.id))) or 0
        )
        for model in (
            InventoryLot,
            InventoryMovement,
            InventoryPallet,
            InventoryPalletItem,
            InventoryReservation,
        )
    }


def get_posting_for_batch(
    db: Session,
    batch_id: int,
) -> InventoryOnboardingPosting | None:
    return db.scalar(
        select(InventoryOnboardingPosting).where(
            InventoryOnboardingPosting.onboarding_batch_id == batch_id
        )
    )


def _eligible_source_lines(
    *,
    batch: InventoryOnboardingBatch,
    all_lines: Iterable[InventoryOnboardingLine],
) -> list[InventoryOnboardingLine]:
    materialized = list(all_lines)
    selected = [
        line
        for line in materialized
        if line.action_decision == "create_new"
        and line.match_status == "ready"
    ]
    if not selected:
        raise _error(
            "该批次没有可正式入账的新库存",
            code="INVENTORY_ONBOARDING_POSTING_EMPTY",
        )
    if len(selected) > MAX_POSTING_LINES:
        raise _error(
            "单批最多正式入账 100 行，请拆成多个库存建账批次",
            code="INVENTORY_ONBOARDING_POSTING_TOO_LARGE",
        )
    if any(
        line.inventory_type not in {"finished", "semi_finished"}
        or line.location_id is None
        or not line.location_code_snapshot
        or not line.area_code_snapshot
        or not line.pallet_code
        or line.quantity is None
        or line.quantity <= 0
        or line.unit not in {"boxes", "sheets"}
        or (
            line.inventory_type == "finished"
            and line.unit != "boxes"
        )
        or (
            line.inventory_type == "semi_finished"
            and line.unit != "sheets"
        )
        or (line.stock_date is None and line.stocktake_date is None)
        for line in selected
    ):
        raise _error(
            "可入账行缺少类型、数量、日期、库位或栈板事实",
            code="INVENTORY_ONBOARDING_POSTING_FACTS_INCOMPLETE",
        )

    selected_ids = {line.id for line in selected}
    selected_pallets = {str(line.pallet_code) for line in selected}
    same_pallet_ids = {
        line.id
        for line in materialized
        if line.pallet_code and str(line.pallet_code) in selected_pallets
    }
    if same_pallet_ids != selected_ids:
        raise _error(
            "同一栈板必须全部属于本批可入账行，不能只入半个栈板",
            code="INVENTORY_ONBOARDING_POSTING_PALLET_INCOMPLETE",
        )

    pallet_types: dict[str, set[str]] = defaultdict(set)
    pallet_locations: dict[str, set[int]] = defaultdict(set)
    location_pallets: dict[int, set[str]] = defaultdict(set)
    for line in selected:
        pallet_code = str(line.pallet_code)
        pallet_types[pallet_code].add(str(line.inventory_type))
        pallet_locations[pallet_code].add(int(line.location_id))
        location_pallets[int(line.location_id)].add(pallet_code)
    if any(len(values) != 1 for values in pallet_types.values()):
        raise _error(
            "同一栈板不能混放成品和半成品",
            code="INVENTORY_ONBOARDING_POSTING_PALLET_TYPE_MIXED",
        )
    if any(len(values) != 1 for values in pallet_locations.values()):
        raise _error(
            "同一栈板不能跨多个库位",
            code="INVENTORY_ONBOARDING_POSTING_PALLET_LOCATION_MIXED",
        )
    if any(len(values) != 1 for values in location_pallets.values()):
        raise _error(
            "一个空库位只能创建一个栈板",
            code="INVENTORY_ONBOARDING_POSTING_LOCATION_MULTIPLE_PALLETS",
        )

    areas = {
        (str(line.floor_snapshot or ""), str(line.area_code_snapshot))
        for line in selected
    }
    if len(areas) != 1:
        raise _error(
            "一个库存建账批次只能正式入账同一楼层、同一区域",
            code="INVENTORY_ONBOARDING_POSTING_AREA_MIXED",
        )
    if batch.resolved_area_code != next(iter(areas))[1]:
        raise _error(
            "批次区域与冻结明细不一致",
            code="INVENTORY_ONBOARDING_POSTING_AREA_STALE",
        )

    for line in selected:
        if line.inventory_type == "finished":
            if line.product_id is None:
                raise _error(
                    "成品入账行必须匹配产品",
                    code="INVENTORY_ONBOARDING_POSTING_PRODUCT_REQUIRED",
                )
            if line.ownership_type == "customer_specific":
                if line.customer_id is None:
                    raise _error(
                        "客户专用成品必须匹配客户",
                        code="INVENTORY_ONBOARDING_POSTING_CUSTOMER_REQUIRED",
                    )
            elif line.ownership_type != "general":
                raise _error(
                    "成品归属类型无效",
                    code="INVENTORY_ONBOARDING_POSTING_OWNER_INVALID",
                )
        else:
            if line.ownership_type == "customer_specific":
                if line.customer_id is None:
                    raise _error(
                        "客户专用半成品必须匹配客户",
                        code="INVENTORY_ONBOARDING_POSTING_CUSTOMER_REQUIRED",
                    )
            elif line.ownership_type == "general":
                if line.customer_id is not None:
                    raise _error(
                        "通用半成品不能带客户归属",
                        code=(
                            "INVENTORY_ONBOARDING_POSTING_"
                            "GENERAL_OWNER_CONFLICT"
                        ),
                    )
            else:
                raise _error(
                    "半成品归属类型无效",
                    code="INVENTORY_ONBOARDING_POSTING_OWNER_INVALID",
                )
    return sorted(selected, key=lambda line: (str(line.pallet_code), line.id))


def _posting_fingerprint(
    batch: InventoryOnboardingBatch,
    lines: Iterable[InventoryOnboardingLine],
) -> str:
    return _json_hash(
        {
            "onboarding_batch_id": batch.id,
            "onboarding_batch_version": batch.version,
            "onboarding_batch_fingerprint": batch.dry_run_fingerprint,
            "line_ids": [line.id for line in lines],
        }
    )


def _lock_and_recheck(
    db: Session,
    *,
    batch: InventoryOnboardingBatch,
    lines: list[InventoryOnboardingLine],
) -> InventoryOnboardingPosting | None:
    location_ids = sorted({int(line.location_id) for line in lines})
    parameters = {
        f"location_{index}": location_id
        for index, location_id in enumerate(location_ids)
    }
    placeholders = ", ".join(f":{key}" for key in parameters)
    db.execute(
        text(
            "UPDATE warehouse_locations SET id = id "
            f"WHERE id IN ({placeholders})"
        ),
        parameters,
    )

    replay = get_posting_for_batch(db, batch.id)
    if replay is not None:
        return replay
    pallet_codes = {str(line.pallet_code) for line in lines}
    source_line_ids = {line.id for line in lines}
    if db.scalar(
        select(InventoryPallet.id)
        .where(
            InventoryPallet.is_current.is_(True),
            InventoryPallet.location_id.in_(location_ids),
        )
        .limit(1)
    ) is not None:
        raise _error(
            "目标库位已有当前栈板，请刷新盘点事实后重新建批",
            code="INVENTORY_ONBOARDING_POSTING_LOCATION_OCCUPIED",
        )
    if db.scalar(
        select(InventoryPallet.id)
        .where(InventoryPallet.pallet_code.in_(pallet_codes))
        .limit(1)
    ) is not None:
        raise _error(
            "目标栈板编号已经存在，请刷新盘点事实后重新建批",
            code="INVENTORY_ONBOARDING_POSTING_PALLET_EXISTS",
        )
    if db.scalar(
        select(InventoryLot.id)
        .where(
            InventoryLot.source_ref_type == "inventory_onboarding_line",
            InventoryLot.source_ref_id.in_(source_line_ids),
        )
        .limit(1)
    ) is not None:
        raise _error(
            "该批次存在已生成正式库存但缺少完整入账记录的来源行",
            code="INVENTORY_ONBOARDING_POSTING_PARTIAL_FACTS",
        )
    return None


def _new_pallets(
    db: Session,
    *,
    batch: InventoryOnboardingBatch,
    lines: Iterable[InventoryOnboardingLine],
    operator: User,
) -> dict[str, InventoryPallet]:
    grouped: dict[str, list[InventoryOnboardingLine]] = defaultdict(list)
    for line in lines:
        grouped[str(line.pallet_code)].append(line)
    now = beijing_now_naive()
    result: dict[str, InventoryPallet] = {}
    for pallet_code, group in grouped.items():
        location_ids = {int(line.location_id) for line in group}
        if len(location_ids) != 1:
            raise _error(
                "栈板跨库位，不能正式入账",
                code="INVENTORY_ONBOARDING_POSTING_PALLET_LOCATION_MIXED",
            )
        location_id = next(iter(location_ids))
        pallet = InventoryPallet(
            pallet_code=pallet_code,
            location_id=location_id,
            status="active",
            is_current=True,
            needs_relocation=False,
            version=1,
            remarks=f"首次库存盘点正式入账 {batch.batch_number}",
            created_by=operator.id,
            updated_by=operator.id,
        )
        db.add(pallet)
        db.flush()
        db.add(
            InventoryLocationMovement(
                pallet_id=pallet.id,
                from_location_id=None,
                to_location_id=location_id,
                movement_type="create",
                operator_id=operator.id,
                moved_at=now,
                idempotency_key=(
                    f"n081-post-b{batch.id}-p{pallet.id}-create"
                ),
                confirmed_at=now,
                pallet_version_before=None,
                pallet_version_after=1,
                remarks=MOVEMENT_REASON,
            )
        )
        result[pallet_code] = pallet
    db.flush()
    return result


def _bind_posted_lot(
    db: Session,
    *,
    batch: InventoryOnboardingBatch,
    source: InventoryOnboardingLine,
    lot: InventoryLot,
    pallet: InventoryPallet,
    operator: User,
) -> InventoryPalletItem:
    if lot.pallet_item is not None:
        if lot.pallet_item.pallet_id != pallet.id:
            raise _error(
                "正式库存已绑定其他栈板",
                code="INVENTORY_ONBOARDING_POSTING_PALLET_BIND_CONFLICT",
            )
        return lot.pallet_item
    if (
        not pallet.is_current
        or pallet.location_id != lot.warehouse_location_id
        or pallet.status != "active"
    ):
        raise _error(
            "正式库存与栈板库位不一致",
            code="INVENTORY_ONBOARDING_POSTING_PALLET_LOCATION_CONFLICT",
        )

    customer_id: int | None = None
    product_id: int | None = None
    inventory_code: str | None = None
    product_name: str | None = None
    customer_name: str | None = None
    if lot.inventory_type == "finished":
        detail = lot.finished_detail
        if detail is None:
            raise _error(
                "成品库存缺少正式详情",
                code="INVENTORY_ONBOARDING_POSTING_FINISHED_DETAIL_MISSING",
            )
        customer_id = detail.owner_customer_id
        product_id = detail.product_id
        inventory_code = detail.inventory_code_snapshot
        product_name = detail.product_name_snapshot
        customer_name = detail.owner_customer_name_snapshot
    else:
        detail = lot.semi_finished_detail
        if detail is None:
            raise _error(
                "半成品库存缺少正式详情",
                code="INVENTORY_ONBOARDING_POSTING_SEMI_DETAIL_MISSING",
            )
        customer_id = detail.owner_customer_id
        inventory_code = detail.material_code_snapshot
        product_name = f"半成品 {detail.material_code_snapshot}"
        customer_name = detail.owner_customer_name_snapshot
    item = InventoryPalletItem(
        pallet=pallet,
        inventory_lot=lot,
        customer_id=customer_id,
        product_id=product_id,
        inventory_code=inventory_code,
        customer_name_snapshot=customer_name,
        product_name=product_name,
        item_type=lot.inventory_type,
        quantity=int(source.quantity),
        unit=lot.unit,
        match_status="matched",
        remarks=f"首次盘点来源行 {source.id}",
        created_by=operator.id,
    )
    before_version = pallet.version
    pallet.version += 1
    pallet.updated_by = operator.id
    now = beijing_now_naive()
    db.add(item)
    db.add(
        InventoryLocationMovement(
            pallet_id=pallet.id,
            from_location_id=pallet.location_id,
            to_location_id=pallet.location_id,
            movement_type="add_item",
            operator_id=operator.id,
            moved_at=now,
            idempotency_key=(
                f"n081-post-b{batch.id}-l{source.id}-bind"
            ),
            confirmed_at=now,
            pallet_version_before=before_version,
            pallet_version_after=pallet.version,
            remarks=MOVEMENT_REASON,
        )
    )
    db.flush()
    return item


def _manual_in(
    db: Session,
    *,
    batch: InventoryOnboardingBatch,
    source: InventoryOnboardingLine,
    pallet: InventoryPallet,
    operator: User,
) -> tuple[InventoryLot, InventoryMovement, InventoryPalletItem]:
    stock_date = source.stock_date or source.stocktake_date
    if stock_date is None:
        raise _error(
            "入账行缺少入库日期",
            code="INVENTORY_ONBOARDING_POSTING_STOCK_DATE_MISSING",
        )
    idempotency_key = f"n081-post-b{batch.id}-l{source.id}-in"
    remarks = f"首次库存盘点；批次 {batch.batch_number}；来源行 {source.id}"
    try:
        if source.inventory_type == "finished":
            is_general = source.ownership_type == "general"
            lot = manual_finished_in(
                db,
                customer_id=(
                    None if is_general else int(source.customer_id)
                ),
                product_id=int(source.product_id),
                location_id=int(source.location_id),
                quantity=int(source.quantity),
                stock_date=stock_date,
                source_type="stocktake",
                source_ref_type="inventory_onboarding_line",
                source_ref_id=source.id,
                pallet_id=pallet.id,
                pallet_code=pallet.pallet_code,
                remarks=remarks,
                operator_id=operator.id,
                idempotency_key=idempotency_key,
                movement_reason=MOVEMENT_REASON,
                stock_date_accuracy=str(source.stock_date_accuracy),
                stock_date_original_text=source.stock_date_original_text,
                is_general=is_general,
            )
        else:
            lot = manual_semi_finished_in(
                db,
                location_id=int(source.location_id),
                quantity=int(source.quantity),
                stock_date=stock_date,
                source_type="stocktake",
                material_id=source.material_id,
                material_code=str(source.material_code_snapshot),
                layer_count=int(source.layer_count),
                flute_type=str(source.flute_type),
                board_length_mm=int(source.board_length_mm),
                board_width_mm=int(source.board_width_mm),
                sheet_type=str(source.sheet_type),
                component_type=str(source.component_type or "whole"),
                pieces_per_box=int(source.pieces_per_box or 1),
                stock_yield_per_sheet=int(
                    source.stock_yield_per_sheet or 1
                ),
                supplier_name=source.supplier_name,
                customer_id=(
                    None
                    if source.ownership_type == "general"
                    else source.customer_id
                ),
                crease_type=source.crease_type,
                crease_left_mm=source.crease_left_mm,
                crease_middle_mm=source.crease_middle_mm,
                crease_right_mm=source.crease_right_mm,
                cutting_note=source.cutting_note,
                remarks=remarks,
                operator_id=operator.id,
                idempotency_key=idempotency_key,
                source_ref_type="inventory_onboarding_line",
                source_ref_id=source.id,
                movement_reason=MOVEMENT_REASON,
                stock_date_accuracy=str(source.stock_date_accuracy),
                stock_date_original_text=source.stock_date_original_text,
            )
    except (TypeError, WarehouseInventoryError) as error:
        raise _error(
            f"正式库存创建失败：{error}",
            status_code=getattr(error, "status_code", 409),
            code="INVENTORY_ONBOARDING_POSTING_MANUAL_IN_FAILED",
        ) from error
    db.flush()
    item = _bind_posted_lot(
        db,
        batch=batch,
        source=source,
        lot=lot,
        pallet=pallet,
        operator=operator,
    )
    movement = db.scalar(
        select(InventoryMovement).where(
            InventoryMovement.idempotency_key == idempotency_key
        )
    )
    if (
        movement is None
        or lot.source_ref_type != "inventory_onboarding_line"
        or lot.source_ref_id != source.id
        or lot.inventory_type != source.inventory_type
        or lot.warehouse_location_id != source.location_id
        or lot.quantity_available != source.quantity
        or lot.unit != source.unit
        or item.pallet_id != pallet.id
    ):
        raise _error(
            "正式库存、流水或栈板事实不完整",
            code="INVENTORY_ONBOARDING_POSTING_FACTS_INCOMPLETE",
        )
    return lot, movement, item


def _operation_log(
    *,
    user: User,
    batch: InventoryOnboardingBatch,
    posting: InventoryOnboardingPosting,
) -> OperationLog:
    return OperationLog(
        user_id=user.id,
        action="N081_POST",
        resource="InventoryOnboardingPosting",
        details=json.dumps(
            {
                "posting_number": posting.posting_number,
                "onboarding_batch_id": batch.id,
                "onboarding_batch_fingerprint": (
                    posting.onboarding_batch_fingerprint
                ),
                "posting_fingerprint": posting.posting_fingerprint,
                "idempotency_key": posting.idempotency_key,
                "line_count": posting.line_count,
                "lot_ids": posting.evidence_json.get("lot_ids", []),
                "pallet_ids": posting.evidence_json.get("pallet_ids", []),
                "movement_ids": posting.evidence_json.get(
                    "movement_ids",
                    [],
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        username=user.username,
        role=user.role,
        entity_type="inventory_onboarding_posting",
        entity_id=posting.id,
        description=MOVEMENT_REASON,
    )


def post_submitted_batch(
    db: Session,
    *,
    batch_id: int,
    operator: User,
) -> InventoryOnboardingPosting:
    replay = get_posting_for_batch(db, batch_id)
    if replay is not None:
        return replay

    batch, all_lines = inventory_onboarding.validate_submitted_onboarding_batch(
        db,
        batch_id=batch_id,
    )
    lines = _eligible_source_lines(batch=batch, all_lines=all_lines)
    posting_fingerprint = _posting_fingerprint(batch, lines)
    replay = _lock_and_recheck(db, batch=batch, lines=lines)
    if replay is not None:
        if (
            replay.onboarding_batch_version != batch.version
            or replay.onboarding_batch_fingerprint
            != batch.dry_run_fingerprint
            or replay.posting_fingerprint != posting_fingerprint
        ):
            raise _error(
                "该批次已有不一致的正式入账记录",
                code="INVENTORY_ONBOARDING_POSTING_REPLAY_CONFLICT",
            )
        return replay

    before_counts = _formal_counts(db)
    pallets = _new_pallets(
        db,
        batch=batch,
        lines=lines,
        operator=operator,
    )
    lot_ids: list[int] = []
    movement_ids: list[int] = []
    pallet_item_ids: list[int] = []
    line_results: list[dict[str, object]] = []
    for source in lines:
        pallet = pallets[str(source.pallet_code)]
        lot, movement, item = _manual_in(
            db,
            batch=batch,
            source=source,
            pallet=pallet,
            operator=operator,
        )
        lot_ids.append(lot.id)
        movement_ids.append(movement.id)
        pallet_item_ids.append(item.id)
        line_results.append(
            {
                "onboarding_line_id": source.id,
                "inventory_lot_id": lot.id,
                "inventory_movement_id": movement.id,
                "inventory_pallet_id": pallet.id,
                "inventory_pallet_item_id": item.id,
            }
        )
    db.flush()
    after_counts = _formal_counts(db)
    now = utc_now_naive()
    posting = InventoryOnboardingPosting(
        posting_number=f"N081-{batch.id}-{posting_fingerprint[:10].upper()}",
        onboarding_batch_id=batch.id,
        onboarding_batch_version=batch.version,
        onboarding_batch_fingerprint=str(batch.dry_run_fingerprint),
        posting_fingerprint=posting_fingerprint,
        idempotency_key=(
            f"n081-post-b{batch.id}-{posting_fingerprint[:16]}"
        ),
        floor_snapshot=(
            str(batch.resolved_floor)
            if batch.resolved_floor is not None
            else None
        ),
        area_code_snapshot=str(batch.resolved_area_code),
        line_count=len(lines),
        finished_line_count=sum(
            line.inventory_type == "finished" for line in lines
        ),
        semi_finished_line_count=sum(
            line.inventory_type == "semi_finished" for line in lines
        ),
        evidence_json={
            "formal_counts_before": before_counts,
            "formal_counts_after": after_counts,
            "lot_ids": sorted(lot_ids),
            "pallet_ids": sorted(pallet.id for pallet in pallets.values()),
            "pallet_item_ids": sorted(pallet_item_ids),
            "movement_ids": sorted(movement_ids),
            "lines": line_results,
        },
        posted_by=operator.id,
        posted_at=now,
    )
    db.add(posting)
    db.flush()
    db.add(_operation_log(user=operator, batch=batch, posting=posting))
    db.flush()
    return posting


def posting_payload(
    posting: InventoryOnboardingPosting,
) -> dict[str, object]:
    evidence = posting.evidence_json or {}
    return {
        "id": posting.id,
        "posting_number": posting.posting_number,
        "onboarding_batch_id": posting.onboarding_batch_id,
        "onboarding_batch_version": posting.onboarding_batch_version,
        "onboarding_batch_fingerprint": (
            posting.onboarding_batch_fingerprint
        ),
        "posting_fingerprint": posting.posting_fingerprint,
        "floor": posting.floor_snapshot,
        "area_code": posting.area_code_snapshot,
        "line_count": posting.line_count,
        "finished_line_count": posting.finished_line_count,
        "semi_finished_line_count": posting.semi_finished_line_count,
        "lot_count": len(evidence.get("lot_ids", [])),
        "pallet_count": len(evidence.get("pallet_ids", [])),
        "movement_count": len(evidence.get("movement_ids", [])),
        "posted_at": utc_naive_to_api(posting.posted_at),
    }


def posting_state_payload(
    db: Session,
    batch: InventoryOnboardingBatch,
) -> dict[str, object]:
    posting = get_posting_for_batch(db, batch.id)
    lines = inventory_onboarding._batch_lines(db, batch.id)
    eligible = [
        line
        for line in lines
        if line.action_decision == "create_new"
        and line.match_status == "ready"
    ]
    return {
        "posting": posting_payload(posting) if posting is not None else None,
        "postable_summary": {
            "line_count": len(eligible),
            "finished_line_count": sum(
                line.inventory_type == "finished" for line in eligible
            ),
            "semi_finished_line_count": sum(
                line.inventory_type == "semi_finished" for line in eligible
            ),
            "pallet_count": len(
                {line.pallet_code for line in eligible if line.pallet_code}
            ),
            "floor": batch.resolved_floor,
            "area_code": batch.resolved_area_code,
            "max_line_count": MAX_POSTING_LINES,
            "can_post": (
                batch.status == "submitted"
                and posting is None
                and 0 < len(eligible) <= MAX_POSTING_LINES
            ),
        },
    }
