"""Validated per-database settings for editable print-template text.

Only static presentation text belongs here.  Order numbers, customers, prices,
quantities, product data, and every other business fact continue to come from
their existing protected APIs.  The settings file deliberately lives beside
the selected SQLite database so isolated UAT copies cannot silently reuse the
formal database's print wording.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Iterator, Mapping

from app.core.config import PROJECT_ROOT, load_settings


_LEGACY_SETTINGS_FILENAME = "print_template_settings.json"
_SETTINGS_FILENAME_PREFIX = "print_template_settings"
_SCHEMA_VERSION = 1
_LOCK = threading.RLock()
_FILE_LOCK_TIMEOUT_SECONDS = 15.0

logger = logging.getLogger(__name__)


class PrintTemplateValidationError(ValueError):
    """The requested template key or field payload is outside the whitelist."""


class PrintTemplateRevisionConflictError(RuntimeError):
    """The caller attempted to overwrite a newer saved template revision."""

    def __init__(self, *, expected_revision: int, current_revision: int) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"打印模板版本冲突：期望 {expected_revision}，当前 {current_revision}"
        )


class PrintTemplateStorageError(RuntimeError):
    """The sidecar settings file could not be read or atomically replaced."""


class PrintTemplateCommitStateUnknownError(PrintTemplateStorageError):
    """The DB audit outcome is unknown; retain the journal for later recovery."""


@dataclass(frozen=True)
class TemplateFieldSpec:
    default: str
    max_length: int = 120


@dataclass(frozen=True)
class PrintTemplateMutation:
    template_key: str
    previous_revision: int
    revision: int
    fields: dict[str, str]
    changed_fields: tuple[str, ...]
    fields_sha256: str
    operation_id: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "template_key": self.template_key,
            "revision": self.revision,
            "fields": dict(self.fields),
        }


def _field(default: str, max_length: int = 120) -> TemplateFieldSpec:
    return TemplateFieldSpec(default=default, max_length=max_length)


_COMMON_FIELDS = {
    "document_title": _field("", 200),
    "address_label": _field("地址："),
    "phone_label": _field("电话："),
    "date_label": _field("日期："),
}


_CONTRACT_CLAUSE_BODIES = {
    "clause_2_body": (
        "甲方在乙方确认规格、数量、图稿及交货信息后，原则上于7个工作日内送达约定地点。"
        "乙方变更资料、地点或拒绝按约收货的，交期相应顺延，增加的仓储、运输等合理费用由乙方承担。"
        "乙方授权人员签收后视为交付完成，货物毁损、灭失风险转由乙方承担；因甲方原因造成的除外。"
    ),
    "clause_3_body": (
        "结算。乙方收到对账单后5个工作日内未书面提出具体异议的，视为对账无异议。"
        "乙方应按约支付货款；逾期的，每日按逾期未付款项万分之三支付违约金，甲方有权暂停后续供货。"
        "甲方依法按实际交易及届时适用税率开具发票。"
    ),
    "clause_4_body": (
        "乙方应在收货时核验数量、规格和外观，并在收货后5个工作日内书面提出异议和证据；"
        "隐蔽质量问题应在发现或应当发现后5个工作日内提出并保留待检货物。"
        "经确认属于甲方责任的，甲方可根据实际情况采取补货、更换、返工、减价或退还不合格部分价款。"
        "因乙方提供或确认的资料、储存、装卸或使用不当造成的问题，由乙方承担。"
    ),
    "clause_5_body": (
        "合同或订单变更、取消须经双方书面确认。乙方在甲方已备料、排产或生产后取消、变更或无正当理由拒收的，"
        "应承担甲方已经发生且可证明的材料、生产、仓储、运输及处置损失。"
        "一方发生法定解除事由，或经催告仍不履行主要义务的，守约方可依法解除；"
        "结算、质量、违约及争议解决条款不因解除或到期而失效。"
    ),
    "clause_6_body": (
        "一方违约应继续履行、采取补救措施并赔偿合理损失。因甲方原因迟延交货的，"
        "按迟延部分货款每日万分之三承担违约金，累计不超过该部分货款的10%。"
        "除法律另有规定外，甲方赔偿范围以乙方能够证明的直接实际损失为限，累计不超过涉争货物价款；"
        "因甲方故意或重大过失造成对方人身损害或财产损失的，不适用该限制。"
    ),
    "clause_7_body": (
        "本合同自双方盖章或有权代表签字之日起生效，一式两份，双方各执一份。"
        "双方确认的订单、报价单、样品、图稿、技术文件、送货单、对账单及补充协议均为合同组成部分；"
        "同一事项不一致的，以后形成且经双方确认的书面文件为准。争议先协商解决；"
        "协商不成的，向甲方住所地有管辖权的人民法院提起诉讼。"
    ),
}


TEMPLATE_FIELD_SPECS: dict[str, dict[str, TemplateFieldSpec]] = {
    "customer_contract": {
        **_COMMON_FIELDS,
        "document_title": _field("购货合同", 200),
        "contract_number_label": _field("合同编号："),
        "supplier_party_label": _field("甲方（供方）："),
        "customer_party_label": _field("乙方（需方）："),
        "contact_phone_label": _field("联系人/电话："),
        "contract_date_label": _field("合同日期："),
        "delivery_date_label": _field("交货日期："),
        "customer_po_label": _field("客户单号 / PO："),
        "article_one_title": _field("第一条　货物名称、规格、数量及价款", 300),
        "column_sequence": _field("序号"),
        "column_product_name": _field("商品名称"),
        "column_specification": _field("规格"),
        "column_material": _field("材质 / 楞型"),
        "column_quantity": _field("数量"),
        "column_unit_price": _field("单价"),
        "column_amount": _field("金额"),
        "column_remarks": _field("备注"),
        "continuation_label": _field("续"),
        "total_amount_label": _field("合同总金额："),
        "contract_remarks_label": _field("合同备注："),
        "contract_remarks_continued_label": _field("合同备注（续）："),
        "clause_2_title": _field("第二条　交货", 200),
        "clause_2_body": _field(_CONTRACT_CLAUSE_BODIES["clause_2_body"], 1000),
        "clause_3_title": _field("第三条　结算与发票", 200),
        "clause_3_payment_prefix": _field("双方按"),
        "clause_3_body": _field(_CONTRACT_CLAUSE_BODIES["clause_3_body"], 1000),
        "clause_4_title": _field("第四条　验收与质量责任", 200),
        "clause_4_body": _field(_CONTRACT_CLAUSE_BODIES["clause_4_body"], 1000),
        "clause_5_title": _field("第五条　变更、解除与终止", 200),
        "clause_5_body": _field(_CONTRACT_CLAUSE_BODIES["clause_5_body"], 1000),
        "clause_6_title": _field("第六条　违约责任", 200),
        "clause_6_body": _field(_CONTRACT_CLAUSE_BODIES["clause_6_body"], 1000),
        "clause_7_title": _field("第七条　生效、附件与争议解决", 200),
        "clause_7_body": _field(_CONTRACT_CLAUSE_BODIES["clause_7_body"], 1000),
        "authorized_representative_label": _field("授权代表："),
        "seal_label": _field("盖章："),
        "signing_date_label": _field("签署日期："),
    },
    "customer_quotation": {
        **_COMMON_FIELDS,
        "document_title": _field("报价单", 200),
        "customer_label": _field("客户："),
        "quotation_number_label": _field("报价单号："),
        "quotation_date_label": _field("报价日期："),
        "column_sequence": _field("序号"),
        "column_temporary_code": _field("临时编码"),
        "column_product_name": _field("产品名称"),
        "column_specification": _field("规格(mm)"),
        "column_material": _field("材质"),
        "column_quantity": _field("数量"),
        "column_unit_price": _field("单价"),
        "column_remarks": _field("备注"),
        "remarks_label": _field("备注："),
        "contact_label": _field("联系人："),
    },
    "legacy_requisition": {
        **_COMMON_FIELDS,
        "document_title": _field("采购单", 200),
        "supplier_label": _field("TO："),
        "number_label": _field("NO："),
        "column_sequence": _field("序号"),
        "column_board_size": _field("纸板长宽（mm）"),
        "column_crease": _field("压线(mm）"),
        "column_material": _field("材质/楞型"),
        "column_quantity": _field("数量"),
        "column_remarks": _field("报料备注"),
    },
    "delivery_note": {
        **_COMMON_FIELDS,
        "document_title": _field("送货单", 200),
        "fax_label": _field("传真："),
        "customer_name_label": _field("客户名称："),
        "delivery_number_label": _field("送货单号："),
        "customer_phone_label": _field("联系电话："),
        "delivery_date_label": _field("送货日期："),
        "customer_address_label": _field("客户地址："),
        "vehicle_number_label": _field("车牌号码："),
        "column_sequence": _field("序号"),
        "column_customer_po": _field("客户单号"),
        "column_product_code": _field("款号"),
        "column_product_name": _field("产品名称"),
        "column_specification": _field("规格"),
        "column_unit": _field("单位"),
        "column_quantity": _field("数量"),
        "column_remarks": _field("备注"),
        "page_quantity_label": _field("本页数量："),
        "total_quantity_label": _field("送货总量："),
        "long_remark_placeholder": _field("详见备注说明"),
        "remark_notes_label": _field("备注说明："),
        "copies_text": _field("白联:存档　红联:客户　黄联:回单", 500),
        "delivery_person_label": _field("送货人："),
        "receiving_unit_label": _field("收货单位(签章)："),
        "handler_label": _field("经手人："),
        "created_at_label": _field("开单时间："),
    },
    "mold_label": {
        **_COMMON_FIELDS,
        "document_title": _field("模具标签", 200),
        "qr_prompt": _field("扫码查询模具"),
        "mold_number_label": _field("模具编号："),
        "product_code_label": _field("存货编码"),
        "specification_label": _field("规格"),
        "additional_products_prefix": _field("另有 "),
        "additional_products_suffix": _field(" 款，请扫码查看。"),
        "board_size_label": _field("片料"),
        "product_size_label": _field("产品"),
        "flute_label": _field("楞型"),
    },
    "customer_list": {
        **_COMMON_FIELDS,
        "document_title": _field("客户列表", 200),
        "column_id": _field("ID"),
        "column_customer_code": _field("客户编号"),
        "column_customer_name": _field("客户名称"),
        "column_contact": _field("联系人"),
        "column_phone": _field("联系电话"),
        "column_address": _field("送货地址"),
        "column_credit_terms": _field("账期"),
        "column_product_count": _field("产品数"),
        "column_history_order_count": _field("历史订单"),
        "column_file_count": _field("文件数"),
        "column_status": _field("状态"),
    },
    "supplier_purchase_order": {
        **_COMMON_FIELDS,
        "document_title": _field("采购单", 200),
        "supplier_label": _field("TO："),
        "number_label": _field("NO："),
        "column_sequence": _field("序号"),
        "column_board_size": _field("纸板长宽（mm）"),
        "column_crease": _field("压线(mm）"),
        "column_material": _field("材质/楞型"),
        "column_quantity": _field("数量"),
        "column_remarks": _field("报料备注"),
    },
    "stock_replenishment_order": {
        **_COMMON_FIELDS,
        "document_title": _field("采购单", 200),
        "supplier_label": _field("TO："),
        "number_label": _field("NO："),
        "column_sequence": _field("序号"),
        "column_board_size": _field("纸板长宽（mm）"),
        "column_crease": _field("压线(mm）"),
        "column_material": _field("材质/楞型"),
        "column_quantity": _field("数量"),
        "column_remarks": _field("报料备注"),
    },
}

TEMPLATE_KEYS = tuple(TEMPLATE_FIELD_SPECS)
DEFAULT_TEMPLATE_FIELDS: dict[str, dict[str, str]] = {
    template_key: {
        field_name: spec.default for field_name, spec in field_specs.items()
    }
    for template_key, field_specs in TEMPLATE_FIELD_SPECS.items()
}


def _selected_database_path() -> Path:
    configured = os.getenv("ERP_DATABASE_PATH", "").strip()
    if configured:
        selected = Path(configured).expanduser()
        if not selected.is_absolute():
            selected = PROJECT_ROOT / selected
        # Deliberately avoid Path.resolve(): on Windows its result can switch
        # between a short and long path while a parent directory is being
        # created concurrently, which would create two identities for one DB.
        return Path(os.path.abspath(os.path.normpath(selected)))
    return Path(os.path.abspath(os.path.normpath(load_settings().database_path)))


def _canonical_database_path(database_path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(database_path)))


def _database_identity(database_path: Path) -> str:
    return hashlib.sha256(
        _canonical_database_path(database_path).encode("utf-8")
    ).hexdigest()


def print_template_settings_path() -> Path:
    """Return a sidecar whose name is unique to the selected database path.

    A directory can contain a live database, a UAT copy, and recovery copies at
    the same time.  A directory-wide fixed filename therefore is not an
    isolation boundary.  The full normalized database path is hashed into the
    filename and is also persisted inside the file so a renamed/copied sidecar
    fails closed instead of leaking wording between database copies.
    """
    database_path = _selected_database_path()
    identity = _database_identity(database_path)
    return database_path.parent / f"{_SETTINGS_FILENAME_PREFIX}.{identity}.json"


def legacy_print_template_settings_path() -> Path:
    """Return the pre-isolation fixed sidecar path (read-only migration source)."""
    return _selected_database_path().parent / _LEGACY_SETTINGS_FILENAME


def _require_template_key(template_key: str) -> dict[str, TemplateFieldSpec]:
    specs = TEMPLATE_FIELD_SPECS.get(template_key)
    if specs is None:
        raise PrintTemplateValidationError("未知的打印模板")
    return specs


def _is_forbidden_character(character: str) -> bool:
    if character == "\n":
        return False
    codepoint = ord(character)
    if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
        return True
    if unicodedata.category(character) in {"Cf", "Cs"}:
        return True
    return 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFF in {0xFFFE, 0xFFFF}


def _normalize_text(
    value: Any,
    *,
    field_name: str,
    spec: TemplateFieldSpec,
) -> str:
    if not isinstance(value, str):
        raise PrintTemplateValidationError(f"字段 {field_name} 必须是纯文本")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > spec.max_length:
        raise PrintTemplateValidationError(
            f"字段 {field_name} 最多允许 {spec.max_length} 个字符"
        )
    if any(_is_forbidden_character(character) for character in normalized):
        raise PrintTemplateValidationError(f"字段 {field_name} 包含不允许的控制字符")
    return normalized


def _normalized_patch(
    template_key: str,
    fields: Mapping[str, Any],
    *,
    allow_empty: bool = False,
) -> dict[str, str]:
    specs = _require_template_key(template_key)
    if not isinstance(fields, Mapping):
        raise PrintTemplateValidationError("fields 必须是对象")
    if not fields and not allow_empty:
        raise PrintTemplateValidationError("至少需要提交一个打印字段")
    unknown = sorted(set(fields) - set(specs))
    if unknown:
        raise PrintTemplateValidationError(
            f"打印模板包含未知字段：{', '.join(unknown)}"
        )
    return {
        field_name: _normalize_text(
            value,
            field_name=field_name,
            spec=specs[field_name],
        )
        for field_name, value in fields.items()
    }


def _default_fields(template_key: str) -> dict[str, str]:
    _require_template_key(template_key)
    return dict(DEFAULT_TEMPLATE_FIELDS[template_key])


def fields_sha256(fields: Mapping[str, str]) -> str:
    canonical = json.dumps(
        dict(fields), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _empty_store(database_identity: str) -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "database_identity": database_identity,
        "templates": {},
    }


def _normalize_stored_template(template_key: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PrintTemplateStorageError("打印模板设置文件结构无效")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise PrintTemplateStorageError("打印模板设置文件版本无效")
    stored_fields = value.get("overrides")
    try:
        patch = _normalized_patch(template_key, stored_fields, allow_empty=True)
    except PrintTemplateValidationError as error:
        raise PrintTemplateStorageError("打印模板设置文件字段无效") from error
    return {"revision": revision, "overrides": patch}


def _normalize_store_payload(
    raw: Any,
    *,
    expected_database_identity: str | None,
    allow_missing_database_identity: bool,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PrintTemplateStorageError("打印模板设置文件结构无效")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != _SCHEMA_VERSION:
        raise PrintTemplateStorageError("打印模板设置文件结构无效")
    database_identity = raw.get("database_identity")
    if database_identity is None and allow_missing_database_identity:
        normalized_identity = expected_database_identity
    elif (
        not isinstance(database_identity, str)
        or len(database_identity) != 64
        or any(character not in "0123456789abcdef" for character in database_identity)
    ):
        raise PrintTemplateStorageError("打印模板设置文件缺少数据库身份")
    else:
        normalized_identity = database_identity
    if (
        expected_database_identity is not None
        and normalized_identity is not None
        and normalized_identity != expected_database_identity
    ):
        raise PrintTemplateStorageError("打印模板设置文件与当前数据库不匹配")
    templates = raw.get("templates")
    if not isinstance(templates, dict):
        raise PrintTemplateStorageError("打印模板设置文件结构无效")
    unknown_templates = sorted(set(templates) - set(TEMPLATE_FIELD_SPECS))
    if unknown_templates:
        raise PrintTemplateStorageError("打印模板设置文件包含未知模板")
    normalized = _empty_store(normalized_identity or "")
    for template_key, value in templates.items():
        normalized["templates"][template_key] = _normalize_stored_template(
            template_key, value
        )
    return normalized


def _parse_store_file_unlocked(
    path: Path,
    *,
    expected_database_identity: str | None,
    allow_missing_database_identity: bool,
) -> dict[str, Any]:
    if not path.is_file():
        raise PrintTemplateStorageError("打印模板设置路径不是文件")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PrintTemplateStorageError("无法读取打印模板设置") from error
    return _normalize_store_payload(
        raw,
        expected_database_identity=expected_database_identity,
        allow_missing_database_identity=allow_missing_database_identity,
    )


def _atomic_replace_json_unlocked(path: Path, store: Mapping[str, Any]) -> None:
    temporary_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(store, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except OSError as error:
        raise PrintTemplateStorageError("无法保存打印模板设置") from error
    finally:
        if temporary_name and os.path.exists(temporary_name):
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak")


def _atomic_write_unlocked(path: Path, store: Mapping[str, Any]) -> None:
    """Atomically replace the primary and keep a best-effort known-good copy."""
    _atomic_replace_json_unlocked(path, store)
    try:
        _atomic_replace_json_unlocked(_backup_path(path), store)
    except PrintTemplateStorageError:
        # The primary replacement is the commit point.  A backup refresh must
        # never turn a successful committed primary into a reported failure.
        logger.warning("无法刷新打印模板设置恢复副本：%s", _backup_path(path))


def _read_store_unlocked(path: Path, database_identity: str) -> dict[str, Any]:
    if not path.exists():
        return _empty_store(database_identity)
    try:
        return _parse_store_file_unlocked(
            path,
            expected_database_identity=database_identity,
            allow_missing_database_identity=False,
        )
    except PrintTemplateStorageError as primary_error:
        backup_path = _backup_path(path)
        if not backup_path.exists():
            raise
        try:
            recovered = _parse_store_file_unlocked(
                backup_path,
                expected_database_identity=database_identity,
                allow_missing_database_identity=False,
            )
            _atomic_write_unlocked(path, recovered)
        except PrintTemplateStorageError:
            raise primary_error
        logger.warning("已从恢复副本修复损坏的打印模板设置：%s", path)
        return recovered


def _legacy_owner_is_selected(database_path: Path) -> bool | None:
    """Return explicit ownership, or None when no owner was configured."""
    configured = os.getenv("ERP_PRINT_TEMPLATE_LEGACY_DATABASE_PATH", "").strip()
    if not configured:
        return None
    owner_path = Path(configured).expanduser()
    if not owner_path.is_absolute():
        raise PrintTemplateStorageError(
            "ERP_PRINT_TEMPLATE_LEGACY_DATABASE_PATH 必须是绝对路径"
        )
    return _database_identity(owner_path) == _database_identity(database_path)


def _database_candidates(database_path: Path) -> list[Path]:
    try:
        entries = database_path.parent.iterdir()
        return [
            entry
            for entry in entries
            if entry.is_file() and entry.suffix.lower() in {".sqlite3", ".sqlite", ".db"}
        ]
    except OSError as error:
        raise PrintTemplateStorageError("无法核对旧打印模板设置归属") from error


def _load_or_migrate_store_unlocked(
    path: Path,
    *,
    database_path: Path,
    database_identity: str,
) -> dict[str, Any]:
    if path.exists():
        return _read_store_unlocked(path, database_identity)

    legacy_path = database_path.parent / _LEGACY_SETTINGS_FILENAME
    if not legacy_path.exists():
        return _empty_store(database_identity)
    legacy = _parse_store_file_unlocked(
        legacy_path,
        expected_database_identity=None,
        allow_missing_database_identity=True,
    )
    legacy_identity = legacy.get("database_identity") or None
    if legacy_identity is not None:
        if legacy_identity != database_identity:
            return _empty_store(database_identity)
    else:
        explicit_owner = _legacy_owner_is_selected(database_path)
        has_wording_overrides = any(
            bool(saved["overrides"]) for saved in legacy["templates"].values()
        )
        if explicit_owner is False:
            return _empty_store(database_identity)
        if explicit_owner is None and has_wording_overrides:
            candidates = _database_candidates(database_path)
            if len(candidates) != 1 or _database_identity(candidates[0]) != database_identity:
                raise PrintTemplateStorageError(
                    "旧打印模板设置归属不明确；请用 "
                    "ERP_PRINT_TEMPLATE_LEGACY_DATABASE_PATH 指定原数据库绝对路径"
                )

    # Copy, do not rename or delete: the fixed legacy sidecar remains available
    # for recovery and for a deliberate migration decision on another machine.
    migrated = deepcopy(legacy)
    migrated["database_identity"] = database_identity
    _atomic_write_unlocked(path, migrated)
    return migrated


@contextmanager
def _interprocess_lock(path: Path) -> Iterator[None]:
    """Serialize read/CAS/write across worker processes using a tiny lock file."""
    lock_path = path.parent / f".{_SETTINGS_FILENAME_PREFIX}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
    except OSError as error:
        raise PrintTemplateStorageError("无法打开打印模板设置锁") from error

    acquired = False
    deadline = time.monotonic() + _FILE_LOCK_TIMEOUT_SECONDS
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise PrintTemplateStorageError(
                        "等待打印模板设置锁超时"
                    ) from error
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.exception("释放打印模板设置锁失败：%s", lock_path)
        handle.close()


@contextmanager
def _store_guard(path: Path) -> Iterator[None]:
    with _LOCK:
        with _interprocess_lock(path):
            yield


def _remove_store_unlocked(path: Path) -> None:
    for candidate in (path, _backup_path(path)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as error:
            raise PrintTemplateStorageError("无法回滚打印模板设置") from error


def _restore_previous_store_unlocked(
    path: Path,
    *,
    existed: bool,
    previous_store: Mapping[str, Any],
) -> None:
    if existed:
        _atomic_write_unlocked(path, previous_store)
    else:
        _remove_store_unlocked(path)


def _selected_store() -> tuple[Path, Path, str]:
    database_path = _selected_database_path()
    database_identity = _database_identity(database_path)
    path = database_path.parent / f"{_SETTINGS_FILENAME_PREFIX}.{database_identity}.json"
    return path, database_path, database_identity


def _transaction_journal_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.txn.json")


def _store_sha256(store: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(store), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _write_transaction_journal_unlocked(
    path: Path,
    *,
    operation_id: str,
    action: str,
    mutation: PrintTemplateMutation,
    previous_existed: bool,
    previous_store: Mapping[str, Any],
    next_store: Mapping[str, Any],
) -> None:
    journal = {
        "schema_version": 1,
        "database_identity": next_store["database_identity"],
        "operation_id": operation_id,
        "action": action,
        "template_key": mutation.template_key,
        "previous_revision": mutation.previous_revision,
        "revision": mutation.revision,
        "changed_fields": list(mutation.changed_fields),
        "fields_sha256": mutation.fields_sha256,
        "previous_existed": previous_existed,
        "previous_store": dict(previous_store),
        "next_store": dict(next_store),
        "previous_store_sha256": _store_sha256(previous_store),
        "next_store_sha256": _store_sha256(next_store),
    }
    _atomic_replace_json_unlocked(_transaction_journal_path(path), journal)


def _remove_transaction_journal_unlocked(path: Path) -> None:
    try:
        _transaction_journal_path(path).unlink(missing_ok=True)
    except OSError as error:
        raise PrintTemplateStorageError("无法清理打印模板设置事务日志") from error


def _require_no_pending_transaction_unlocked(path: Path) -> None:
    if _transaction_journal_path(path).exists():
        raise PrintTemplateStorageError("打印模板设置存在待恢复事务")


def _read_transaction_journal_unlocked(
    path: Path,
    *,
    database_identity: str,
) -> dict[str, Any]:
    journal_path = _transaction_journal_path(path)
    try:
        raw = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PrintTemplateStorageError("无法读取打印模板设置事务日志") from error
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise PrintTemplateStorageError("打印模板设置事务日志结构无效")
    operation_id = raw.get("operation_id")
    if (
        not isinstance(operation_id, str)
        or len(operation_id) != 32
        or any(character not in "0123456789abcdef" for character in operation_id)
    ):
        raise PrintTemplateStorageError("打印模板设置事务编号无效")
    if raw.get("database_identity") != database_identity:
        raise PrintTemplateStorageError("打印模板设置事务与当前数据库不匹配")
    if raw.get("action") not in {"PRINT_TEMPLATE_UPDATE", "PRINT_TEMPLATE_RESTORE"}:
        raise PrintTemplateStorageError("打印模板设置事务动作无效")
    if not isinstance(raw.get("previous_existed"), bool):
        raise PrintTemplateStorageError("打印模板设置事务结构无效")
    previous_store = _normalize_store_payload(
        raw.get("previous_store"),
        expected_database_identity=database_identity,
        allow_missing_database_identity=False,
    )
    next_store = _normalize_store_payload(
        raw.get("next_store"),
        expected_database_identity=database_identity,
        allow_missing_database_identity=False,
    )
    if raw.get("previous_store_sha256") != _store_sha256(previous_store):
        raise PrintTemplateStorageError("打印模板设置事务前态校验失败")
    if raw.get("next_store_sha256") != _store_sha256(next_store):
        raise PrintTemplateStorageError("打印模板设置事务后态校验失败")
    return {
        "operation_id": operation_id,
        "previous_existed": raw["previous_existed"],
        "previous_store": previous_store,
        "next_store": next_store,
    }


def recover_interrupted_print_template_transaction(
    audit_committed: Callable[[str], bool],
) -> dict[str, str] | None:
    """Resolve a crash window against the durable OperationLog.

    The write-ahead journal is fsynced before the primary sidecar changes.  If
    its operation id exists in OperationLog, recovery completes the new state;
    otherwise it restores the exact pre-write state.  Thus a process exit at
    any point between file replacement and database commit is deterministic.
    """
    path, _, database_identity = _selected_store()
    with _store_guard(path):
        if not _transaction_journal_path(path).exists():
            return None
        journal = _read_transaction_journal_unlocked(
            path,
            database_identity=database_identity,
        )
        try:
            committed = bool(audit_committed(journal["operation_id"]))
        except Exception as error:
            raise PrintTemplateStorageError(
                "无法核对打印模板设置审计状态"
            ) from error
        if committed:
            _atomic_write_unlocked(path, journal["next_store"])
            outcome = "committed"
        else:
            _restore_previous_store_unlocked(
                path,
                existed=journal["previous_existed"],
                previous_store=journal["previous_store"],
            )
            outcome = "rolled_back"
        _remove_transaction_journal_unlocked(path)
        return {"operation_id": journal["operation_id"], "outcome": outcome}


def get_print_template_settings(template_key: str) -> dict[str, Any]:
    """Read a merged template without creating a sidecar on first access."""
    _require_template_key(template_key)
    path, database_path, database_identity = _selected_store()
    with _store_guard(path):
        _require_no_pending_transaction_unlocked(path)
        store = _load_or_migrate_store_unlocked(
            path,
            database_path=database_path,
            database_identity=database_identity,
        )
        saved = store["templates"].get(template_key)
        fields = _default_fields(template_key)
        if saved:
            fields.update(saved["overrides"])
        return {
            "template_key": template_key,
            "revision": saved["revision"] if saved else 0,
            "fields": fields,
        }


@contextmanager
def staged_save_print_template_settings(
    template_key: str,
    *,
    expected_revision: int,
    fields: Mapping[str, Any],
) -> Iterator[PrintTemplateMutation]:
    """Stage a CAS update and compensate it if the surrounding audit fails.

    The file lock remains held across the caller's database commit.  Therefore
    another process cannot advance the revision between the file replacement
    and a compensating rollback.
    """
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise PrintTemplateValidationError("expected_revision 必须是非负整数")
    if expected_revision < 0:
        raise PrintTemplateValidationError("expected_revision 必须是非负整数")
    patch = _normalized_patch(template_key, fields)
    path, database_path, database_identity = _selected_store()
    with _store_guard(path):
        _require_no_pending_transaction_unlocked(path)
        store = _load_or_migrate_store_unlocked(
            path,
            database_path=database_path,
            database_identity=database_identity,
        )
        existed = path.exists()
        previous_store = deepcopy(store)
        saved = store["templates"].get(template_key)
        current_revision = saved["revision"] if saved else 0
        if expected_revision != current_revision:
            raise PrintTemplateRevisionConflictError(
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        defaults = _default_fields(template_key)
        previous_fields = dict(defaults)
        if saved:
            previous_fields.update(saved["overrides"])
        next_fields = dict(previous_fields)
        next_fields.update(patch)
        changed_fields = tuple(
            sorted(
                field_name
                for field_name in patch
                if previous_fields[field_name] != next_fields[field_name]
            )
        )
        if not changed_fields:
            yield PrintTemplateMutation(
                template_key=template_key,
                previous_revision=current_revision,
                revision=current_revision,
                fields=next_fields,
                changed_fields=(),
                fields_sha256=fields_sha256(next_fields),
            )
            return
        next_revision = current_revision + 1
        store["templates"][template_key] = {
            "revision": next_revision,
            "overrides": {
                field_name: value
                for field_name, value in next_fields.items()
                if value != defaults[field_name]
            },
        }
        operation_id = uuid.uuid4().hex
        mutation = PrintTemplateMutation(
            template_key=template_key,
            previous_revision=current_revision,
            revision=next_revision,
            fields=next_fields,
            changed_fields=changed_fields,
            fields_sha256=fields_sha256(next_fields),
            operation_id=operation_id,
        )
        _write_transaction_journal_unlocked(
            path,
            operation_id=operation_id,
            action="PRINT_TEMPLATE_UPDATE",
            mutation=mutation,
            previous_existed=existed,
            previous_store=previous_store,
            next_store=store,
        )
        try:
            _atomic_write_unlocked(path, store)
        except BaseException:
            try:
                _remove_transaction_journal_unlocked(path)
            except PrintTemplateStorageError:
                logger.exception("清理未生效的打印模板设置事务日志失败")
            raise
        try:
            yield mutation
        except PrintTemplateCommitStateUnknownError:
            logger.error(
                "打印模板审计结果未知，保留事务日志等待恢复：%s",
                operation_id,
            )
            raise
        except BaseException:
            _restore_previous_store_unlocked(
                path,
                existed=existed,
                previous_store=previous_store,
            )
            try:
                _remove_transaction_journal_unlocked(path)
            except PrintTemplateStorageError:
                logger.exception("清理已回滚的打印模板设置事务日志失败")
            raise
        else:
            try:
                _remove_transaction_journal_unlocked(path)
            except PrintTemplateStorageError:
                # The committed audit plus journal operation id make this
                # recoverable on the next API request; do not report failure
                # after both durable states already agree.
                logger.exception("延迟清理已提交的打印模板设置事务日志")


def save_print_template_settings(
    template_key: str,
    *,
    expected_revision: int,
    fields: Mapping[str, Any],
) -> PrintTemplateMutation:
    """Persist without an external audit transaction (service-level callers)."""
    with staged_save_print_template_settings(
        template_key,
        expected_revision=expected_revision,
        fields=fields,
    ) as mutation:
        return mutation


@contextmanager
def staged_restore_print_template_settings(
    template_key: str,
    *,
    expected_revision: int,
) -> Iterator[PrintTemplateMutation]:
    """Stage a restore and compensate it if the surrounding audit fails."""
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise PrintTemplateValidationError("expected_revision 必须是非负整数")
    if expected_revision < 0:
        raise PrintTemplateValidationError("expected_revision 必须是非负整数")
    _require_template_key(template_key)
    path, database_path, database_identity = _selected_store()
    with _store_guard(path):
        _require_no_pending_transaction_unlocked(path)
        store = _load_or_migrate_store_unlocked(
            path,
            database_path=database_path,
            database_identity=database_identity,
        )
        existed = path.exists()
        previous_store = deepcopy(store)
        saved = store["templates"].get(template_key)
        current_revision = saved["revision"] if saved else 0
        if expected_revision != current_revision:
            raise PrintTemplateRevisionConflictError(
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        previous_fields = _default_fields(template_key)
        if saved:
            previous_fields.update(saved["overrides"])
        next_fields = _default_fields(template_key)
        next_revision = current_revision + 1
        store["templates"][template_key] = {
            "revision": next_revision,
            "overrides": {},
        }
        changed_fields = tuple(
            sorted(
                field_name
                for field_name in next_fields
                if previous_fields[field_name] != next_fields[field_name]
            )
        )
        operation_id = uuid.uuid4().hex
        mutation = PrintTemplateMutation(
            template_key=template_key,
            previous_revision=current_revision,
            revision=next_revision,
            fields=next_fields,
            changed_fields=changed_fields,
            fields_sha256=fields_sha256(next_fields),
            operation_id=operation_id,
        )
        _write_transaction_journal_unlocked(
            path,
            operation_id=operation_id,
            action="PRINT_TEMPLATE_RESTORE",
            mutation=mutation,
            previous_existed=existed,
            previous_store=previous_store,
            next_store=store,
        )
        try:
            _atomic_write_unlocked(path, store)
        except BaseException:
            try:
                _remove_transaction_journal_unlocked(path)
            except PrintTemplateStorageError:
                logger.exception("清理未生效的打印模板设置事务日志失败")
            raise
        try:
            yield mutation
        except PrintTemplateCommitStateUnknownError:
            logger.error(
                "打印模板审计结果未知，保留事务日志等待恢复：%s",
                operation_id,
            )
            raise
        except BaseException:
            _restore_previous_store_unlocked(
                path,
                existed=existed,
                previous_store=previous_store,
            )
            try:
                _remove_transaction_journal_unlocked(path)
            except PrintTemplateStorageError:
                logger.exception("清理已回滚的打印模板设置事务日志失败")
            raise
        else:
            try:
                _remove_transaction_journal_unlocked(path)
            except PrintTemplateStorageError:
                logger.exception("延迟清理已提交的打印模板设置事务日志")


def restore_print_template_settings(
    template_key: str,
    *,
    expected_revision: int,
) -> PrintTemplateMutation:
    """Persist a restore without an external audit transaction."""
    with staged_restore_print_template_settings(
        template_key,
        expected_revision=expected_revision,
    ) as mutation:
        return mutation
