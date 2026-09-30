"""Customer-scoped common-box drawing drafts and immutable releases."""
from __future__ import annotations

import hashlib
import json
import math
import os
from functools import wraps
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, get_db, require_customer_access
from app.core.time_contract import beijing_today
from app.models.drawing_design import DrawingDesign, DrawingNumberSequence, DrawingRelease
from app.models.product import Product
from app.models.customer import Customer
from app.models.mold_tool import MoldTool
from app.models.product_drawing import ProductDrawing
from app.models.user import User
from app.services.drawing_exports import dxf, engineering_pdf, engineering_pdf_1to1
from app.services.drawing_snapshots import read_svg_snapshot
from app.services.drawing_artwork import inspect_artwork, prepare_artwork
from app.services.drawing_text import render_text_artwork
from app.services.drawing_switch import drawing_v2_enabled, require_drawing_v2_write
from app.services.drawing_product_rules import slotted_parameter_candidates, validate_single_piece_slotted
from app.services.drawing_geometry import DrawingGeometryError, PARAMETER_KEYS, build_geometry, svg, number, custom_parameter_suggestions, print_focus_geometry
from app.services.drawing_workbench import (CATALOG_VERSION, fold_model, preview_payload,
                                            split_editor_state, template_catalog,
                                            validate_editor_state, with_editor_state)
from app.services.secure_uploads import (ValidatedUpload, remove_stored_reference,
                                         resolve_stored_reference, store_private_upload)


router = APIRouter()
read = PermissionChecker("products.view")
write = PermissionChecker("products.edit")
TEMPLATES = {"liner_v1", "slotted_v1", "custom_21301634_v1"}
THICKNESS = {"AB": ("7", False), "BE": ("4", False), "A": ("4", False),
             "B": ("3", False), "E": ("1", False)}


@router.get("/drawing-workbench/catalog")
def workbench_catalog(user: User = Depends(read)) -> dict:
    """Static, permission-gated template capabilities; never reads product facts."""
    del user
    return template_catalog()


class PrintObject(BaseModel):
    kind: str = Field(pattern="^(text|image)$")
    text: str | None = Field(default=None, max_length=200)
    source_drawing_id: int | None = Field(default=None, gt=0)
    image_bounds: str = Field(default="canvas", pattern="^(canvas|alpha)$")
    size_confirmed: bool = False
    panel_id: str = Field(min_length=1, max_length=40)
    x_mm: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    y_mm: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    width_mm: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    height_mm: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    rotation_deg: Decimal = Field(default=Decimal(0), ge=-360, le=360)


class DesignWrite(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=12, max_length=100)
    expected_product_version: int = Field(ge=1)
    expected_design_version: int | None = Field(default=None, ge=1)
    template_key: str
    parameters: dict[str, Decimal | None]
    print_objects: list[PrintObject] = Field(default_factory=list, max_length=30)
    paper_color: str = Field(default="white", pattern="^(white|natural)$")
    thickness_mm: Decimal | None = Field(default=None, gt=0, max_digits=6, decimal_places=2)
    thickness_source: str | None = Field(default=None, max_length=160)
    thickness_approximate: bool = False
    customer_number: str | None = Field(default=None, max_length=160)
    customer_revision: str | None = Field(default=None, max_length=50)
    editor_state: dict[str, Any] | None = None


class WorkbenchPreviewWrite(BaseModel):
    template_key: str
    parameters: dict[str, Decimal | None]
    editor_state: dict[str, Any] | None = None


class PublishWrite(BaseModel):
    expected_product_version: int = Field(ge=1)
    expected_design_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=12, max_length=100)


def _transaction_errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        db = kwargs.get("db") or args[2]
        try:
            return function(*args, **kwargs)
        except (IntegrityError, OperationalError) as error:
            db.rollback()
            raise HTTPException(409, "并发操作冲突，请使用原请求键重试") from error
        except OSError as error:
            db.rollback()
            raise HTTPException(503, "图纸存储暂不可用，未发布成功，可使用原请求键重试") from error
        except Exception:
            db.rollback()
            raise
    return wrapped


def _lock_product(db: Session, product: Product, expected: int, user: User) -> None:
    changed = db.execute(update(Product).where(Product.id == product.id, Product.version == expected)
                         .values(version=Product.version, updated_at=Product.updated_at))
    if changed.rowcount != 1:
        raise HTTPException(409, "常用箱已变化，请重新读取")
    db.refresh(product)
    require_customer_access(product.customer_id, current_user=user, db=db)


def _product(db: Session, product_id: int, user: User) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(404, "常用箱不存在")
    require_customer_access(product.customer_id, current_user=user, db=db)
    return product


def _json(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _published_result(release: DrawingRelease, customer_id: int) -> dict:
    if release.customer_id != customer_id:
        raise HTTPException(409, "发布版客户与当前产品不一致，请核对历史归属")
    if not os.getenv("ERP_FILE_STORAGE_DIR"):
        raise HTTPException(503, "图纸存储根未显式配置")
    try:
        path = resolve_stored_reference(release.pdf_reference)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != release.pdf_sha256:
            raise HTTPException(503, "发布文件缺失或校验失败，请核对后重试")
        manifest = json.loads(release.manifest_json)
        _released_assets(manifest.get('print_objects', []))
        for layer in ('structure', 'print'):
            read_svg_snapshot(manifest, layer)
    except OSError as error:
        raise HTTPException(503, "发布文件暂不可读，请核对后重试") from error
    return {"id": release.id, "number": release.external_number, "revision": release.revision}


def _params(product: Product, template: str, params: dict) -> dict:
    result, _ = split_editor_state(params)
    if template == "liner_v1":
        result = {"length_mm": product.length_mm, "width_mm": product.width_mm}
    elif template == "custom_21301634_v1":
        # The verified central panel comes from the common-box product fields;
        # all other folds and covers remain independent per product.
        result["panel_width_mm"] = product.length_mm
        result["panel_height_mm"] = product.width_mm
    return result


def _geometry(product: Product, design: DrawingDesign) -> dict:
    try:
        if design.template_key == "slotted_v1":
            validate_single_piece_slotted(product)
        return build_geometry(design.template_key, _params(product, design.template_key,
                                                             json.loads(design.parameters_json)))
    except DrawingGeometryError as error:
        raise HTTPException(422, str(error)) from error


def _draft_parameters(design: DrawingDesign) -> tuple[dict, dict | None]:
    try:
        return split_editor_state(json.loads(design.parameters_json))
    except DrawingGeometryError as error:
        raise HTTPException(422, str(error)) from error


def _validate_component_state(db: Session, product: Product, state: dict | None, *, for_publish: bool) -> dict | None:
    if state is None or not state.get("assembly"):
        return state
    try:
        from app.services.drawing_components import component_context, validate_component_placements
    except ImportError as error:
        raise HTTPException(503, "组合图纸校验组件尚未就绪") from error
    assembly = dict(state["assembly"])
    try:
        if not assembly.get("basis_hash"):
            assembly["basis_hash"] = component_context(db, product)["basis_hash"]
        assembly["placements"] = validate_component_placements(
            db, product, assembly.get("placements", []),
            expected_basis_hash=assembly.get("basis_hash"), for_publish=for_publish)
    except DrawingGeometryError as error:
        raise HTTPException(422, str(error)) from error
    return {**state, "assembly": assembly}


def _objects(design: DrawingDesign, geometry: dict) -> list[dict]:
    objects = json.loads(design.print_objects_json)
    panels = {panel["id"]: panel for panel in geometry["panels"]}
    for obj in objects:
        panel = panels.get(obj["panel_id"])
        if panel is None:
            raise HTTPException(422, "印刷对象面板不存在")
        x, y, w, h = (float(obj[key]) for key in ("x_mm", "y_mm", "width_mm", "height_mm"))
        angle = math.radians(float(obj.get("rotation_deg", 0)))
        bound_w = abs(w * math.cos(angle)) + abs(h * math.sin(angle))
        bound_h = abs(w * math.sin(angle)) + abs(h * math.cos(angle))
        if x + bound_w > float(panel["width"]) + 0.00001 or y + bound_h > float(panel["height"]) + 0.00001:
            raise HTTPException(422, "印刷内容旋转后超出所属面板")
    return objects


def _image_source(db: Session, product_id: int, source_id: int) -> bytes:
    if not os.getenv("ERP_FILE_STORAGE_DIR"):
        raise HTTPException(503, "图纸存储根未显式配置")
    source = db.get(ProductDrawing, source_id)
    if source is None or source.product_id != product_id:
        raise HTTPException(422, "印刷图片不属于当前常用箱")
    path = resolve_stored_reference(source.image_path)
    if not path.is_file() or path.stat().st_size > 5_000_000:
        raise HTTPException(422, "印刷图片缺失或超过5MB")
    return path.read_bytes()


def _image_bytes(db: Session, product_id: int, obj: dict) -> tuple[bytes, str, str]:
    content, extension, mime, _ = prepare_artwork(_image_source(db, product_id, obj.get("source_drawing_id")), obj)
    return content, extension, mime


@router.get("/{product_id}/managed-drawing/assets/{drawing_id}")
def inspect_print_asset(product_id: int, drawing_id: int,
                        db: Session = Depends(get_db), user: User = Depends(read)) -> dict:
    _product(db, product_id, user)
    info = inspect_artwork(_image_source(db, product_id, drawing_id))
    source = db.get(ProductDrawing, drawing_id)
    return {**info, "original_preserved": source.image_path.startswith("private:drawing_originals/")}


def _draft_assets(db: Session, product_id: int, objects: list[dict]) -> dict[int, tuple[bytes, str]]:
    assets = {}
    for index, obj in enumerate(objects):
        if obj["kind"] == "image":
            content, _, mime = _image_bytes(db, product_id, obj)
            assets[index] = (content, mime)
        else:
            content, obj['text_rendering'] = render_text_artwork(obj)
            assets[index] = (content, 'image/png')
    return assets


def _released_assets(objects: list[dict]) -> dict[int, tuple[bytes, str]]:
    if not os.getenv("ERP_FILE_STORAGE_DIR"):
        raise HTTPException(503, "图纸存储根未显式配置")
    assets = {}
    for index, obj in enumerate(objects):
        if obj["kind"] != "image" and not obj.get('asset_reference'):
            continue
        path = resolve_stored_reference(obj["asset_reference"])
        if not path.is_file() or path.stat().st_size > 5_000_000:
            raise HTTPException(503, "发布图片缺失或超过5MB")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != obj["asset_sha256"]:
            raise HTTPException(503, "发布图片校验失败")
        if obj.get("original_reference"):
            original = resolve_stored_reference(obj["original_reference"])
            if (not original.is_file() or original.stat().st_size > 5_000_000
                    or hashlib.sha256(original.read_bytes()).hexdigest() != obj["original_sha256"]):
                raise HTTPException(503, "发布原件缺失、超过5MB或校验失败")
        assets[index] = (content, obj["asset_mime"])
    return assets


def _thickness(product: Product, request: DesignWrite) -> tuple[str | None, str | None, bool]:
    if request.thickness_mm is not None:
        if not request.thickness_source:
            raise HTTPException(422, "人工纸厚需要填写来源")
        return str(request.thickness_mm), request.thickness_source, request.thickness_approximate
    if product.layer_count == 7:
        return "9", "工厂默认：七层约9mm（2026-09-21）", True
    flute = str(product.flute_type or "").upper()
    if flute in THICKNESS:
        value, approximate = THICKNESS[flute]
        return value, f"工厂默认：{flute}瓦（2026-09-21）", approximate
    return None, None, False


@router.get("/{product_id}/managed-drawing")
def get_design(product_id: int, db: Session = Depends(get_db), user: User = Depends(read)) -> dict:
    product = _product(db, product_id, user)
    design = db.get(DrawingDesign, product.id)
    if design is None:
        return {"draft": None, "releases": [], "editing_enabled": drawing_v2_enabled()}
    releases = db.scalars(select(DrawingRelease).where(DrawingRelease.product_id == product.id,
                                                      DrawingRelease.customer_id == product.customer_id)
                          .order_by(DrawingRelease.id.desc())).all()
    geometry_error, panels = None, []
    publication_outdated = False
    try:
        geometry = _geometry(product, design)
        panels = geometry["panels"]
        _objects(design, geometry)
        publication_outdated = any(r.design_version == design.version and r.product_version == product.version
                                   and json.loads(r.manifest_json).get('geometry') != geometry for r in releases)
        if design.source_product_version != product.version:
            geometry_error = "常用箱已变化，请重新核对并保存草稿"
    except HTTPException as error:
        geometry_error = str(error.detail)
    parameters, editor_state = _draft_parameters(design)
    return {"editing_enabled": drawing_v2_enabled(), "draft": {"template_key": design.template_key,
                       "parameters": parameters, "editor_state": editor_state,
                       "print_objects": json.loads(design.print_objects_json),
                       "paper_color": design.paper_color,
                       "thickness_mm": design.thickness_mm,
                       "thickness_source": design.thickness_source,
                       "thickness_approximate": bool(design.thickness_approximate),
                       "customer_number": design.customer_number,
                       "customer_revision": design.customer_revision,
                       "internal_number": design.internal_number,
                       "version": design.version,
                       "source_product_version": design.source_product_version},
            "geometry_ready": geometry_error is None,
            "publication_outdated": publication_outdated,
            "geometry_error": geometry_error, "panels": panels,
            "releases": [{"id": r.id, "revision": r.revision,
                          "number": r.external_number, "published_at": r.published_at}
                         for r in releases]}


@router.get("/{product_id}/managed-drawing/parameter-suggestions")
def suggest_parameters(product_id: int, db: Session = Depends(get_db), user: User = Depends(read),
                       template_key: str = "custom_21301634_v1") -> dict:
    product = _product(db, product_id, user)
    try:
        if template_key == "slotted_v1":
            return {'product_version': product.version, 'template_key': template_key,
                    'parameters': slotted_parameter_candidates(product),
                    'basis': '单片A1/0201名义尺寸初值，独立于人工报料；槽宽按实际填写',
                    'manual_parameters': ['slot_width_mm']}
        if template_key != "custom_21301634_v1":
            raise HTTPException(422, "该模板不需要分段初值")
        values = custom_parameter_suggestions(product.length_mm, product.width_mm, product.height_mm)
    except DrawingGeometryError as error:
        raise HTTPException(422, str(error)) from error
    return {'product_version': product.version, 'template_key': 'custom_21301634_v1',
            'parameters': values, 'basis': '2026-09-22用户确认：摇盖初值=产品宽/2；左右折边初值=模切后箱高',
            'manual_parameters': ['top_fold_mm', 'bottom_fold_mm', 'left_wing_mm', 'right_wing_mm']}


@router.get("/{product_id}/managed-drawing/workbench-context")
def workbench_context(product_id: int, db: Session = Depends(get_db), user: User = Depends(read)) -> dict:
    """Read-only workbench state; product dimensions remain nominal source facts."""
    product = _product(db, product_id, user)
    design = db.get(DrawingDesign, product.id)
    draft = None
    if design is not None:
        parameters, editor_state = _draft_parameters(design)
        draft = {"design_version": design.version, "template_key": design.template_key,
                 "parameters": parameters, "editor_state": editor_state}
    releases = db.scalars(select(DrawingRelease).where(DrawingRelease.product_id == product.id,
                          DrawingRelease.customer_id == product.customer_id).order_by(DrawingRelease.id.desc())).all()
    component_context = None
    try:
        from app.services.drawing_components import component_context as load_component_context
        component_context = load_component_context(db, product)
    except ImportError:
        # This branch is deliberately usable before the independently-owned
        # read-only component projection is integrated.
        component_context = None
    return {"catalog_version": CATALOG_VERSION, "unit": "mm",
            "product": {"id": product.id, "version": product.version, "customer_id": product.customer_id,
                        "nominal_dimensions_mm": {"length": str(product.length_mm) if product.length_mm is not None else None,
                                                  "width": str(product.width_mm) if product.width_mm is not None else None,
                                                  "height": str(product.height_mm) if product.height_mm is not None else None},
                        "drawing_basis_note": "产品和采购尺寸未被图纸工作台改写"},
            "draft": draft, "component_context": component_context,
            "release_summaries": [{"id": release.id, "number": release.external_number,
                                    "revision": release.revision, "design_version": release.design_version,
                                    "published_at": release.published_at} for release in releases]}


@router.post("/{product_id}/managed-drawing/workbench-preview")
def workbench_preview(product_id: int, payload: WorkbenchPreviewWrite,
                      db: Session = Depends(get_db), user: User = Depends(read)) -> dict:
    """Build a non-persistent preview from the same validated geometry source."""
    product = _product(db, product_id, user)
    if payload.template_key not in TEMPLATES:
        raise HTTPException(422, "尚未验证该结构模板")
    try:
        if payload.template_key == "slotted_v1":
            validate_single_piece_slotted(product)
        parameters = _params(product, payload.template_key, payload.parameters)
        state = validate_editor_state(payload.editor_state)
        result = preview_payload(payload.template_key, parameters, state)
        result["svg"] = svg(result["geometry"])
        return result
    except DrawingGeometryError as error:
        raise HTTPException(422, str(error)) from error


@router.put("/{product_id}/managed-drawing")
@_transaction_errors
def save_design(product_id: int, payload: DesignWrite,
                db: Session = Depends(get_db), user: User = Depends(write)) -> dict:
    require_drawing_v2_write()
    product = _product(db, product_id, user)
    digest = hashlib.sha256(_json(payload.model_dump(mode="json")).encode()).hexdigest()
    existing = db.get(DrawingDesign, product.id)
    if existing and payload.idempotency_key and existing.last_save_key == payload.idempotency_key:
        if existing.last_save_hash != digest:
            raise HTTPException(409, "同一保存请求键不能用于不同内容")
        return get_design(product_id, db, user)
    _lock_product(db, product, payload.expected_product_version, user)
    if product.version != payload.expected_product_version:
        raise HTTPException(409, "常用箱已被其他人修改，请重新读取")
    if payload.template_key not in TEMPLATES:
        raise HTTPException(422, "尚未验证该结构模板")
    try:
        editor_state = _validate_component_state(db, product, validate_editor_state(payload.editor_state), for_publish=False)
        resolved = _params(product, payload.template_key, payload.parameters)
        for key, value in resolved.items():
            if value is not None:
                number(value, key)
        missing = [key for key in PARAMETER_KEYS[payload.template_key] if resolved.get(key) is None]
        geometry = None if missing else build_geometry(payload.template_key, resolved)
    except DrawingGeometryError as error:
        raise HTTPException(422, str(error)) from error
    params = {key: str(value) if value is not None else None for key, value in payload.parameters.items()}
    if payload.template_key == "liner_v1":
        params = {}  # no second editable copy of the product dimensions
    if payload.template_key == "custom_21301634_v1":
        params.pop("panel_width_mm", None)
        params.pop("panel_height_mm", None)
    if editor_state is None and existing is not None:
        _, editor_state = _draft_parameters(existing)
    params = with_editor_state(params, editor_state)
    objects = [item.model_dump(mode="json") for item in payload.print_objects]
    # Validate on save, not only when rendering.
    probe = DrawingDesign(product_id=product.id, template_key=payload.template_key,
                          parameters_json=_json(params), print_objects_json=_json(objects))
    if geometry is not None:
        _objects(probe, geometry)
    elif objects:
        raise HTTPException(422, "结构分段尚未完善，请先保存结构草稿再配置印刷")
    _draft_assets(db, product.id, objects)
    thickness, source, approximate = _thickness(product, payload)
    design = db.get(DrawingDesign, product.id, populate_existing=True)
    if payload.thickness_mm is None and design is not None and design.thickness_mm is not None:
        thickness, source, approximate = design.thickness_mm, design.thickness_source, design.thickness_approximate
    if design and payload.idempotency_key and design.last_save_key == payload.idempotency_key:
        if design.last_save_hash != digest:
            raise HTTPException(409, "同一保存请求键不能用于不同内容")
        return get_design(product_id, db, user)
    fields = dict(template_key=payload.template_key, parameters_json=_json(params),
                  print_objects_json=_json(objects), paper_color=payload.paper_color,
                  thickness_mm=thickness, thickness_source=source,
                  thickness_approximate=int(approximate),
                  source_product_version=product.version,
                  last_save_key=payload.idempotency_key, last_save_hash=digest,
                  customer_number=payload.customer_number if (payload.customer_number or "").strip() else None)
    fields["customer_revision"] = payload.customer_revision if (payload.customer_revision or "").strip() else None
    if design is None:
        if payload.expected_design_version is not None:
            raise HTTPException(409, "图纸草稿版本已变化")
        design = DrawingDesign(product_id=product.id, version=1, **fields)
        db.add(design)
    else:
        if design.version != payload.expected_design_version:
            raise HTTPException(409, "图纸草稿已被其他人修改")
        changed = db.execute(update(DrawingDesign).where(DrawingDesign.product_id == product.id,
                                                        DrawingDesign.version == payload.expected_design_version)
                             .values(**fields, version=design.version + 1))
        if changed.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "图纸草稿已被其他人修改")
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "并发保存冲突，请重新读取") from error
    return get_design(product_id, db, user)


@router.get("/{product_id}/managed-drawing/preview.svg")
def preview_design(product_id: int, db: Session = Depends(get_db), user: User = Depends(read)) -> Response:
    product = _product(db, product_id, user)
    design = db.get(DrawingDesign, product.id)
    if design is None:
        raise HTTPException(404, "尚未保存图纸草稿")
    geometry = _geometry(product, design)
    objects = _objects(design, geometry)
    assets = _draft_assets(db, product.id, objects)
    return Response(svg(geometry, objects, assets), media_type="image/svg+xml",
                    headers={"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'none'"})


@router.post("/{product_id}/managed-drawing/releases")
@_transaction_errors
def publish_design(product_id: int, payload: PublishWrite,
                   db: Session = Depends(get_db), user: User = Depends(write)) -> dict:
    require_drawing_v2_write()
    product = _product(db, product_id, user)
    previous = db.scalar(select(DrawingRelease).where(DrawingRelease.product_id == product.id,
                                                       DrawingRelease.idempotency_key == payload.idempotency_key))
    if previous is not None:
        if previous.product_version != payload.expected_product_version or previous.design_version != payload.expected_design_version:
            raise HTTPException(409, "同一发布请求键不能用于不同版本")
        return _published_result(previous, product.customer_id)
    _lock_product(db, product, payload.expected_product_version, user)
    previous = db.scalar(select(DrawingRelease).where(DrawingRelease.product_id == product.id,
                         DrawingRelease.idempotency_key == payload.idempotency_key))
    if previous is not None:
        if previous.product_version != payload.expected_product_version or previous.design_version != payload.expected_design_version:
            raise HTTPException(409, "同一发布请求键不能用于不同版本")
        return _published_result(previous, product.customer_id)
    design = db.get(DrawingDesign, product.id, populate_existing=True)
    if design is None or design.version != payload.expected_design_version or product.version != payload.expected_product_version or design.source_product_version != product.version:
        raise HTTPException(409, "产品或图纸草稿已变化，请重新核对")
    published = db.scalar(select(DrawingRelease).where(DrawingRelease.product_id == product.id,
                           DrawingRelease.design_version == design.version,
                           DrawingRelease.product_version == product.version))
    if published is not None:
        if json.loads(published.manifest_json).get('geometry') != _geometry(product, design):
            raise HTTPException(409, '图纸规则已更新，请保存草稿、核对预览后发布新版；旧版仍保留')
        return _published_result(published, product.customer_id)
    if not os.getenv("ERP_FILE_STORAGE_DIR"):
        raise HTTPException(503, "图纸存储根未显式配置，禁止发布")
    stored_parameters, stored_editor_state = _draft_parameters(design)
    frozen_editor_state = _validate_component_state(db, product, stored_editor_state, for_publish=True)
    if frozen_editor_state is not None and frozen_editor_state.get("dimension_basis", "dieline") != "dieline":
        raise HTTPException(422, "内尺寸或外尺寸尚无已确认加工补偿；请核实并按刀线/压线尺寸保存后发布")
    geometry = _geometry(product, design)
    objects = _objects(design, geometry)
    if design.thickness_mm is None:
        raise HTTPException(422, "纸厚及来源尚未确定，暂不能发布工程图")
    if not design.internal_number:
        today = beijing_today().strftime("%Y%m%d")
        serial = db.scalar(sqlite_insert(DrawingNumberSequence).values(business_date=today,last_number=1)
                           .on_conflict_do_update(index_elements=["business_date"],
                                                  set_={"last_number": DrawingNumberSequence.last_number + 1})
                           .returning(DrawingNumberSequence.last_number))
        design.internal_number = f"TM-DWG-{today}-{serial:03d}"
    revision_number = db.query(DrawingRelease).filter(DrawingRelease.product_id == product.id).count()
    revision = design.customer_revision or f"R{revision_number:02d}"
    number = design.customer_number or design.internal_number
    if db.scalar(select(DrawingRelease.id).where(DrawingRelease.product_id == product.id,
            DrawingRelease.external_number == number, DrawingRelease.revision == revision)) is not None:
        raise HTTPException(409, "该图号和版次已发布；修改内容请填写新客户版次，原发布版保留")
    thickness = f"{'约' if design.thickness_approximate else ''}{design.thickness_mm}mm（{design.thickness_source}）"
    stored_references = []
    assets = {}
    try:
        for index, obj in enumerate(objects):
            if obj['kind'] == 'text':
                content, obj['text_rendering'] = render_text_artwork(obj)
                copied = store_private_upload(ValidatedUpload(content=content, original_filename=f'print-text-{index}.png',
                    extension='.png', content_type='image/png', size=len(content),
                    sha256=hashlib.sha256(content).hexdigest()), category='managed_drawing_assets')
                stored_references.append(copied.reference)
                obj['asset_reference'], obj['asset_sha256'], obj['asset_mime'] = copied.reference, copied.sha256, 'image/png'
                assets[index] = (content, 'image/png')
                continue
            if obj["kind"] != "image":
                continue
            if not obj.get("size_confirmed"):
                raise HTTPException(422, "请确认图片所选范围的实际印刷宽高；无尺度照片不能直接作为生产尺寸")
            original_content = _image_source(db, product.id, obj.get("source_drawing_id"))
            content, ext, mime, info = prepare_artwork(original_content, obj)
            obj["source_pixels"] = info
            source = db.get(ProductDrawing, obj["source_drawing_id"])
            obj["source_original_preserved"] = source.image_path.startswith("private:drawing_originals/")
            if content != original_content:
                original_ext, original_mime = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"), "WEBP": (".webp", "image/webp")}[info["format"]]
                original = store_private_upload(ValidatedUpload(content=original_content,
                    original_filename=f"original-{index}{original_ext}", extension=original_ext,
                    content_type=original_mime, size=len(original_content),
                    sha256=hashlib.sha256(original_content).hexdigest()), category="managed_drawing_assets")
                stored_references.append(original.reference)
                obj["original_reference"], obj["original_sha256"] = original.reference, original.sha256
            copied = store_private_upload(ValidatedUpload(content=content, original_filename=f"print-source-{index}{ext}",
                                         extension=ext, content_type=mime, size=len(content),
                                         sha256=hashlib.sha256(content).hexdigest()), category="managed_drawing_assets")
            stored_references.append(copied.reference)
            obj["asset_reference"] = copied.reference
            obj["asset_sha256"] = copied.sha256
            obj["asset_mime"] = mime
            assets[index] = (content, mime)
    except Exception:
        for reference in stored_references:
            remove_stored_reference(reference)
        raise
    mold = db.get(MoldTool, product.mold_tool_id) if product.mold_tool_id else None
    customer = db.get(Customer, product.customer_id)
    manifest = {"template": design.template_key, "parameters": with_editor_state(stored_parameters, frozen_editor_state),
                "material_snapshot": {"id": product.material_id,
                    "code": product.material.code if product.material else None,
                    "flute_type": product.flute_type, "layer_count": product.layer_count},
                "printing_snapshot": {"mode": product.printing_plate_mode,
                    "content": product.print_content,
                    "plate_ids": [product.printing_plate_1_id, product.printing_plate_2_id, product.printing_plate_3_id],
                    "plate_codes": [plate.plate_code for plate in
                        (product.printing_plate_1, product.printing_plate_2, product.printing_plate_3) if plate]},
                "product_dimensions": {"length_mm": str(product.length_mm),
                                       "width_mm": str(product.width_mm),
                                       "height_mm": str(product.height_mm)},
                "geometry": geometry, "fold_model": fold_model(design.template_key, geometry),
                "print_objects": objects, "paper_color": design.paper_color,
                "customer_name": customer.name if customer else str(product.customer_id),
                "thickness": thickness, "product_version": product.version,
                "mold_tool_id": product.mold_tool_id,
                "mold_snapshot": ({"code": mold.mold_code, "name": mold.mold_name,
                                   "location_at_publish": mold.rack_location} if mold else None)}
    try:
        manifest['svg_snapshots'] = {}
        print_view = print_focus_geometry(geometry, objects)
        if objects:
            manifest['print_view'] = {'kind': 'print_focus', 'view_bounds': print_view['view_bounds']}
        for layer in ('structure', 'print'):
            rendered = svg(print_view if layer == 'print' else geometry,
                           objects if layer == 'print' else [], assets).encode('utf-8')
            if len(rendered) > 40_000_000:
                raise HTTPException(422, '印刷原件合计过大，请缩小文件后重试')
            frozen = store_private_upload(ValidatedUpload(content=rendered, original_filename=f'{layer}.svg',
                                         extension='.svg', content_type='image/svg+xml', size=len(rendered),
                                         sha256=hashlib.sha256(rendered).hexdigest()), category='managed_drawings')
            stored_references.append(frozen.reference)
            manifest['svg_snapshots'][layer] = {'reference': frozen.reference, 'sha256': frozen.sha256}
        content = engineering_pdf(geometry, customer=manifest['customer_name'],
                                  product=product.product_name, number=number,
                                  revision=revision, thickness=thickness, print_objects=objects,
                                  image_assets=assets)
        saved = store_private_upload(ValidatedUpload(content=content, original_filename="drawing.pdf",
                                                       extension=".pdf", content_type="application/pdf",
                                                       size=len(content), sha256=hashlib.sha256(content).hexdigest()),
                                     category="managed_drawings")
        stored_references.append(saved.reference)
    except Exception:
        for reference in stored_references:
            remove_stored_reference(reference)
        raise
    release = DrawingRelease(product_id=product.id, customer_id=product.customer_id,
                             revision=revision, external_number=number,
                             internal_number=design.internal_number,
                             idempotency_key=payload.idempotency_key,
                             design_version=design.version, product_version=product.version,
                             template_key=design.template_key, manifest_json=_json(manifest),
                             pdf_reference=saved.reference, pdf_sha256=saved.sha256,
                             mold_tool_id=product.mold_tool_id, published_by=user.id)
    db.add(release)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        for reference in stored_references:
            remove_stored_reference(reference)
        raise HTTPException(409, "图号或版次发生并发冲突，请用同一请求键重试") from error
    except Exception:
        db.rollback()
        # A driver can lose its response after COMMIT reached the database.
        # Confirm durable state before deleting any file used by that commit.
        try:
            with Session(bind=db.get_bind()) as verification:
                durable = verification.scalar(select(DrawingRelease).where(
                    DrawingRelease.product_id == product_id,
                    DrawingRelease.idempotency_key == payload.idempotency_key))
                if durable is not None:
                    return _published_result(durable, product.customer_id)
        except Exception as error:
            raise HTTPException(503, "发布提交结果待核实，文件已保留，请使用同一请求键重试") from error
        for reference in stored_references:
            remove_stored_reference(reference)
        raise
    return {"id": release.id, "number": number, "revision": revision}


@router.get("/{product_id}/managed-drawing/releases/{release_id}/file")
def download_release(product_id: int, release_id: int,
                     db: Session = Depends(get_db), user: User = Depends(read)) -> FileResponse:
    product = _product(db, product_id, user)
    release = db.get(DrawingRelease, release_id)
    if release is None or release.product_id != product.id or release.customer_id != product.customer_id:
        raise HTTPException(404, "图纸版本不存在")
    if not os.getenv("ERP_FILE_STORAGE_DIR"):
        raise HTTPException(503, "图纸存储根未显式配置")
    path = resolve_stored_reference(release.pdf_reference)
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != release.pdf_sha256:
        raise HTTPException(503, "发布文件缺失或校验失败")
    return FileResponse(path, media_type="application/pdf", headers={"Cache-Control": "private, no-store"})


@router.get("/{product_id}/managed-drawing/releases/{release_id}/export")
def export_release(product_id: int, release_id: int, format: str,
                   db: Session = Depends(get_db), user: User = Depends(read)) -> Response:
    """Vector exchange files are always rendered from the immutable release manifest."""
    product = _product(db, product_id, user)
    release = db.get(DrawingRelease, release_id)
    if release is None or release.product_id != product.id or release.customer_id != product.customer_id:
        raise HTTPException(404, "图纸版本不存在")
    manifest = json.loads(release.manifest_json)
    geometry = manifest.get("geometry")
    if not isinstance(geometry, dict):
        raise HTTPException(503, "发布图纸快照不完整")
    if format == "svg":
        content, media_type, extension = svg(geometry).encode("utf-8"), "image/svg+xml", "svg"
    elif format == "pdf_1to1":
        content, media_type, extension = engineering_pdf_1to1(geometry), "application/pdf", "pdf"
    elif format == "dxf":
        content, media_type, extension = dxf(geometry), "application/dxf", "dxf"
    else:
        raise HTTPException(422, "仅支持 svg、pdf_1to1 或 dxf 导出")
    filename = f"{release.external_number}-{release.revision}.{extension}".replace('"', '')
    return Response(content, media_type=media_type, headers={"Cache-Control": "private, no-store",
                    "Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{product_id}/managed-drawing/releases/{release_id}/{layer}.svg")
def release_svg(product_id: int, release_id: int, layer: str,
                db: Session = Depends(get_db), user: User = Depends(read)) -> Response:
    product = _product(db, product_id, user)
    release = db.get(DrawingRelease, release_id)
    if release is None or release.product_id != product.id or release.customer_id != product.customer_id:
        raise HTTPException(404, "图纸版本不存在")
    if layer not in {"structure", "print"}:
        raise HTTPException(404, "图层不存在")
    manifest = json.loads(release.manifest_json)
    objects = manifest["print_objects"] if layer == "print" else []
    rendered = read_svg_snapshot(manifest, layer)
    if rendered is None:
        rendered = svg(manifest["geometry"], objects, _released_assets(objects))
    return Response(rendered, media_type="image/svg+xml",
                    headers={"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'none'",
                             "Cache-Control": "private, no-store"})
