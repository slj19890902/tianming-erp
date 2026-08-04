from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
import hmac
import os
from pathlib import Path
import tempfile
from uuid import uuid4

import ezdxf
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import Base, create_database_engine, create_session_factory
from .dxf_parser import parse_layout_dxf
from .layout_rules import RULE_DEFAULTS_MM, evaluate_layout_rules, feature_area_mm2
from .models import (
    AssetTemplate,
    EquipmentPlacement,
    Layout,
    LayoutFeature,
    RackPlacement,
)
from .schemas import (
    AssetTemplateCreate,
    AssetTemplateRead,
    LayoutRead,
    LayoutSummary,
    FeatureCreate,
    FeatureRead,
    FeatureUpdate,
    PlacementCreate,
    PlacementRead,
    PlacementUpdate,
    RackCreate,
    RackRead,
    RackUpdate,
)


DEFAULT_ASSETS = (
    ("水性印刷机", "印刷", "#2563eb", 9000, 2600, 2400),
    ("模切机", "模切", "#7c3aed", 4200, 2800, 2200),
    ("半自动粘箱机", "成型", "#059669", 6500, 1600, 1800),
    ("钉箱机", "成型", "#d97706", 1800, 1600, 2100),
    ("分纸机", "分切", "#dc2626", 3200, 1800, 1900),
)
MAX_DXF_BYTES = 25 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024


def _ensure_phase2a_schema(engine) -> None:
    """Add compatible layout fields without moving or rewriting existing objects."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    statements: list[str] = []
    if "twin_equipment_placements" in tables:
        placement_columns = {
            column["name"]
            for column in inspector.get_columns("twin_equipment_placements")
        }
        if "is_confirmed" not in placement_columns:
            statements.append(
                "ALTER TABLE twin_equipment_placements "
                "ADD COLUMN is_confirmed BOOLEAN NOT NULL DEFAULT FALSE"
            )
        if "is_locked" not in placement_columns:
            statements.append(
                "ALTER TABLE twin_equipment_placements "
                "ADD COLUMN is_locked BOOLEAN NOT NULL DEFAULT FALSE"
            )
    if "twin_layout_features" in tables:
        feature_columns = {
            column["name"] for column in inspector.get_columns("twin_layout_features")
        }
        if "storage_mode" not in feature_columns:
            statements.append(
                "ALTER TABLE twin_layout_features "
                "ADD COLUMN storage_mode VARCHAR(20) NOT NULL DEFAULT 'floor'"
            )
        if "elevation_mm" not in feature_columns:
            statements.append(
                "ALTER TABLE twin_layout_features "
                "ADD COLUMN elevation_mm FLOAT NOT NULL DEFAULT 0"
            )
        if "storage_height_mm" not in feature_columns:
            statements.append(
                "ALTER TABLE twin_layout_features "
                "ADD COLUMN storage_height_mm FLOAT NOT NULL DEFAULT 1000"
            )
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))


def _layout_summary(layout: Layout) -> dict:
    return {
        "id": layout.id,
        "name": layout.name,
        "floor_code": layout.floor_code,
        "source_name": layout.source_name,
        "source_sha256": layout.source_sha256,
        "source_units": layout.source_units,
        "bounds_mm": layout.bounds_json,
        "created_at": layout.created_at,
        "updated_at": layout.updated_at,
    }


def _asset_read(asset: AssetTemplate) -> dict:
    return {
        "id": asset.id,
        "name": asset.name,
        "category": asset.category,
        "render_type": asset.render_type,
        "image_url": asset.image_url,
        "color": asset.color,
        "default_width_mm": asset.default_width_mm,
        "default_depth_mm": asset.default_depth_mm,
        "default_height_mm": asset.default_height_mm,
    }


def _placement_read(placement: EquipmentPlacement) -> dict:
    return {
        "id": placement.id,
        "layout_id": placement.layout_id,
        "template_id": placement.template_id,
        "name": placement.name,
        "x_mm": placement.x_mm,
        "y_mm": placement.y_mm,
        "z_mm": placement.z_mm,
        "width_mm": placement.width_mm,
        "depth_mm": placement.depth_mm,
        "height_mm": placement.height_mm,
        "rotation_deg": placement.rotation_deg,
        "is_confirmed": placement.is_confirmed,
        "is_locked": placement.is_locked,
        "version": placement.version,
    }


def _rack_read(rack: RackPlacement) -> dict:
    return {
        "id": rack.id,
        "layout_id": rack.layout_id,
        "rack_code": rack.rack_code,
        "name": rack.name,
        "x_mm": rack.x_mm,
        "y_mm": rack.y_mm,
        "z_mm": rack.z_mm,
        "width_mm": rack.width_mm,
        "depth_mm": rack.depth_mm,
        "height_mm": rack.height_mm,
        "levels": rack.levels,
        "bays": rack.bays,
        "access_side": rack.access_side,
        "min_aisle_width_mm": rack.min_aisle_width_mm,
        "rotation_deg": rack.rotation_deg,
        "color": rack.color,
        "source": rack.source,
        "status": rack.status,
        "is_locked": rack.is_locked,
        "version": rack.version,
    }


def _feature_read(feature: LayoutFeature) -> dict:
    return {
        "id": feature.id,
        "layout_id": feature.layout_id,
        "feature_code": feature.feature_code,
        "name": feature.name,
        "feature_kind": feature.feature_kind,
        "subtype": feature.subtype,
        "points": feature.points_json,
        "width_mm": feature.width_mm,
        "direction": feature.direction,
        "no_stacking": feature.no_stacking,
        "storage_mode": feature.storage_mode,
        "elevation_mm": feature.elevation_mm,
        "storage_height_mm": feature.storage_height_mm,
        "color": feature.color,
        "area_mm2": feature.area_mm2,
        "source": feature.source,
        "status": feature.status,
        "version": feature.version,
    }


def _layout_read(layout: Layout) -> dict:
    return {
        **_layout_summary(layout),
        "structures": layout.structures_json,
        "warnings": layout.warnings_json,
        "placements": [_placement_read(item) for item in layout.placements],
        "racks": [_rack_read(item) for item in layout.racks],
        "features": [_feature_read(item) for item in layout.features],
        "violations": evaluate_layout_rules(layout),
        "rule_defaults": RULE_DEFAULTS_MM,
    }


def create_app(
    *,
    database_url: str | None = None,
    data_dir: Path | None = None,
    editor_token: str | None = None,
) -> FastAPI:
    root = Path(__file__).resolve().parents[1]
    runtime_dir = (data_dir or root / "data").resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir = runtime_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    db_url = database_url or os.getenv(
        "FACTORY_TWIN_DATABASE_URL",
        f"sqlite+pysqlite:///{(runtime_dir / 'factory_twin.sqlite3').as_posix()}",
    )
    expected_token = editor_token or os.getenv(
        "FACTORY_TWIN_EDITOR_TOKEN", "local-mvp-token"
    )
    engine = create_database_engine(db_url)
    session_factory = create_session_factory(engine)

    def get_session():
        with session_factory() as session:
            yield session

    def require_editor_token(x_editor_token: str | None = Header(default=None)) -> None:
        if not x_editor_token or not hmac.compare_digest(x_editor_token, expected_token):
            raise HTTPException(status_code=403, detail="缺少或无效的布局编辑令牌")

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        Base.metadata.create_all(engine)
        _ensure_phase2a_schema(engine)
        with session_factory() as session:
            for name, category, color, width, depth, height in DEFAULT_ASSETS:
                exists = session.scalar(select(AssetTemplate).where(AssetTemplate.name == name))
                if exists is None:
                    session.add(
                        AssetTemplate(
                            name=name,
                            category=category,
                            render_type="box25d",
                            color=color,
                            default_width_mm=width,
                            default_depth_mm=depth,
                            default_height_mm=height,
                        )
                    )
            demo_digest = "demo-layout-v1".ljust(64, "0")
            demo = session.scalar(
                select(Layout).where(Layout.source_sha256 == demo_digest)
            )
            if demo is None:
                demo_structures = [
                    {
                        "id": "DEMO-WALL-1",
                        "source_handle": "DEMO-WALL-1",
                        "kind": "exterior_wall",
                        "layer": "外墙",
                        "locked": True,
                        "source_readonly": True,
                        "geometry": {
                            "type": "polyline",
                            "closed": True,
                            "points": [[0, 0], [20000, 0], [20000, 12000], [0, 12000]],
                        },
                    },
                    *[
                        {
                            "id": f"DEMO-COLUMN-{index}",
                            "source_handle": f"DEMO-COLUMN-{index}",
                            "kind": "column",
                            "layer": "柱子",
                            "locked": True,
                            "source_readonly": True,
                            "column_code": f"COL-1F-{index:03d}",
                            "geometry": {
                                "type": "circle",
                                "x_mm": x,
                                "y_mm": y,
                                "radius_mm": 300,
                            },
                        }
                        for index, (x, y) in enumerate(
                            ((5000, 3000), (15000, 3000), (5000, 9000), (15000, 9000)),
                            start=1,
                        )
                    ],
                    {
                        "id": "DEMO-DOOR-1",
                        "source_handle": "DEMO-DOOR-1",
                        "kind": "door",
                        "layer": "门",
                        "locked": False,
                        "source_readonly": True,
                        "geometry": {
                            "type": "polyline",
                            "closed": False,
                            "points": [[8500, 0], [11500, 0]],
                        },
                    },
                ]
                demo = Layout(
                        name="MVP 演示厂房（模拟数据）",
                        floor_code="1F",
                        source_name="demo_factory.dxf",
                        source_sha256=demo_digest,
                        source_units="mm",
                        bounds_json={"min_x": 0, "min_y": 0, "max_x": 20000, "max_y": 12000},
                        structures_json=demo_structures,
                        warnings_json=["这是用于页面预览的模拟底图，请导入真实 DXF 后再布置正式设备草稿。"],
                    )
                session.add(demo)
            session.flush()
            demo_placement = session.scalar(
                select(EquipmentPlacement).where(EquipmentPlacement.layout_id == demo.id)
            )
            if demo_placement is None:
                demo_equipment_specs = (
                    ("水性印刷机", "1号水性印刷机", 5600, 3900, 0),
                    ("模切机", "1号模切机", 14800, 3900, 90),
                    ("半自动粘箱机", "1号半自动粘箱机", 14500, 9200, 0),
                )
                for asset_name, placement_name, x_mm, y_mm, rotation in demo_equipment_specs:
                    template = session.scalar(
                        select(AssetTemplate).where(AssetTemplate.name == asset_name)
                    )
                    if template is not None:
                        session.add(
                            EquipmentPlacement(
                                layout_id=demo.id,
                                template_id=template.id,
                                name=placement_name,
                                x_mm=x_mm,
                                y_mm=y_mm,
                                width_mm=template.default_width_mm,
                                depth_mm=template.default_depth_mm,
                                height_mm=template.default_height_mm,
                                rotation_deg=rotation,
                            )
                        )
            demo_rack = session.scalar(
                select(RackPlacement).where(RackPlacement.layout_id == demo.id)
            )
            if demo_rack is None:
                session.add(
                    RackPlacement(
                        layout_id=demo.id,
                        rack_code="RACK-1F-MOLD-001",
                        name="三层模具货架",
                        x_mm=5000,
                        y_mm=6000,
                        width_mm=3200,
                        depth_mm=900,
                        height_mm=2400,
                        levels=3,
                        bays=4,
                        access_side="south",
                        min_aisle_width_mm=1800,
                        rotation_deg=0,
                        color="#8b5cf6",
                        source="manual",
                        status="candidate",
                    )
                )
            demo_feature = session.scalar(
                select(LayoutFeature).where(LayoutFeature.layout_id == demo.id)
            )
            if demo_feature is None:
                feature_specs = (
                    (
                        "ZONE-1F-RAW-001",
                        "纸板待生产区",
                        "zone",
                        "raw_material",
                        [[500, 500], [7000, 500], [7000, 2500], [500, 2500]],
                        None,
                        None,
                        False,
                        "#60a5fa",
                    ),
                    (
                        "ZONE-1F-SEMI-001",
                        "半成品待加工区",
                        "zone",
                        "semi_finished",
                        [[8000, 500], [13000, 500], [13000, 2500], [8000, 2500]],
                        None,
                        None,
                        False,
                        "#f59e0b",
                    ),
                    (
                        "AISLE-1F-FORK-001",
                        "主叉车通道",
                        "aisle",
                        "forklift",
                        [[1000, 6000], [19000, 6000]],
                        3000,
                        "two_way",
                        True,
                        "#22c55e",
                    ),
                    (
                        "NO-GO-1F-FIRE-001",
                        "南门消防出口禁放区",
                        "no_go",
                        "fire_exit",
                        [[8000, 0], [12000, 0], [12000, 2500], [8000, 2500]],
                        None,
                        None,
                        True,
                        "#ef4444",
                    ),
                )
                for code, name, kind, subtype, points, width, direction, no_stacking, color in feature_specs:
                    session.add(
                        LayoutFeature(
                            layout_id=demo.id,
                            feature_code=code,
                            name=name,
                            feature_kind=kind,
                            subtype=subtype,
                            points_json=points,
                            width_mm=width,
                            direction=direction,
                            no_stacking=no_stacking,
                            color=color,
                            area_mm2=feature_area_mm2(kind, points, width),
                            source="manual",
                            status="candidate",
                        )
                    )
            session.commit()
        yield
        engine.dispose()

    app = FastAPI(
        title="天明工厂数字孪生布局编辑器 MVP",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5174", "http://localhost:5174"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.mount("/uploads", StaticFiles(directory=uploads_dir), name="twin-uploads")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "database": engine.dialect.name, "source_write": False}

    @app.get("/api/layouts", response_model=list[LayoutSummary])
    def list_layouts(session: Session = Depends(get_session)):
        layouts = session.scalars(select(Layout).order_by(Layout.updated_at.desc())).all()
        return [_layout_summary(item) for item in layouts]

    @app.get("/api/layouts/{layout_id}", response_model=LayoutRead)
    def get_layout(layout_id: str, session: Session = Depends(get_session)):
        layout = session.get(Layout, layout_id)
        if layout is None:
            raise HTTPException(status_code=404, detail="布局不存在")
        return _layout_read(layout)

    @app.post(
        "/api/layouts/import-dxf",
        response_model=LayoutRead,
        dependencies=[Depends(require_editor_token)],
    )
    async def import_dxf(
        file: UploadFile = File(...),
        name: str = Form(...),
        floor_code: str = Form("1F"),
        session: Session = Depends(get_session),
    ):
        if not file.filename or Path(file.filename).suffix.lower() != ".dxf":
            raise HTTPException(status_code=400, detail="只接受 .dxf 文件")
        content = await file.read(MAX_DXF_BYTES + 1)
        if len(content) > MAX_DXF_BYTES:
            raise HTTPException(status_code=413, detail="DXF 文件不能超过 25MB")
        digest = sha256(content).hexdigest()
        normalized_floor = floor_code.strip().upper()
        existing = session.scalar(
            select(Layout).where(
                Layout.source_sha256 == digest,
                Layout.floor_code == normalized_floor,
            )
        )
        if existing is not None:
            return _layout_read(existing)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".dxf", dir=runtime_dir, delete=False
            ) as handle:
                handle.write(content)
                temp_path = Path(handle.name)
            parsed = parse_layout_dxf(temp_path, floor_code=normalized_floor)
        except (ValueError, ezdxf.DXFError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        layout = Layout(
            name=name.strip() or f"{normalized_floor} 工厂布局",
            floor_code=normalized_floor,
            source_name=Path(file.filename).name,
            source_sha256=digest,
            source_units=parsed["source"]["input_units"],
            bounds_json=parsed["bounds_mm"],
            structures_json=parsed["structures"],
            warnings_json=parsed["warnings"],
        )
        session.add(layout)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(Layout).where(
                    Layout.source_sha256 == digest,
                    Layout.floor_code == normalized_floor,
                )
            )
            if existing is None:
                raise
            return _layout_read(existing)
        session.refresh(layout)
        return _layout_read(layout)

    @app.get("/api/assets", response_model=list[AssetTemplateRead])
    def list_assets(session: Session = Depends(get_session)):
        assets = session.scalars(select(AssetTemplate).order_by(AssetTemplate.category, AssetTemplate.name)).all()
        return [_asset_read(item) for item in assets]

    @app.post(
        "/api/assets",
        response_model=AssetTemplateRead,
        dependencies=[Depends(require_editor_token)],
    )
    async def create_asset(
        name: str = Form(...),
        category: str = Form("生产设备"),
        render_type: str = Form("box25d"),
        color: str = Form("#2563eb"),
        default_width_mm: float = Form(...),
        default_depth_mm: float = Form(...),
        default_height_mm: float = Form(...),
        image: UploadFile | None = File(default=None),
        session: Session = Depends(get_session),
    ):
        try:
            validated = AssetTemplateCreate(
                name=name,
                category=category,
                render_type=render_type,
                color=color,
                default_width_mm=default_width_mm,
                default_depth_mm=default_depth_mm,
                default_height_mm=default_height_mm,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        image_url = None
        if validated.render_type == "png":
            if image is None or not image.filename:
                raise HTTPException(status_code=422, detail="PNG 素材必须上传透明背景图片")
            content = await image.read(MAX_PNG_BYTES + 1)
            if len(content) > MAX_PNG_BYTES:
                raise HTTPException(status_code=413, detail="PNG 素材不能超过 8MB")
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise HTTPException(status_code=422, detail="素材文件不是有效 PNG")
            stored_name = f"{uuid4().hex}.png"
            (uploads_dir / stored_name).write_bytes(content)
            image_url = f"/uploads/{stored_name}"
        asset = AssetTemplate(**validated.model_dump(), image_url=image_url)
        session.add(asset)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="素材名称已存在") from error
        session.refresh(asset)
        return _asset_read(asset)

    @app.post(
        "/api/layouts/{layout_id}/placements",
        response_model=PlacementRead,
        dependencies=[Depends(require_editor_token)],
    )
    def create_placement(
        layout_id: str,
        body: PlacementCreate,
        session: Session = Depends(get_session),
    ):
        layout = session.get(Layout, layout_id)
        template = session.get(AssetTemplate, body.template_id)
        if layout is None:
            raise HTTPException(status_code=404, detail="布局不存在")
        if template is None:
            raise HTTPException(status_code=404, detail="设备素材不存在")
        placement = EquipmentPlacement(
            layout_id=layout.id,
            template_id=template.id,
            name=body.name or template.name,
            x_mm=body.x_mm,
            y_mm=body.y_mm,
            z_mm=body.z_mm,
            width_mm=body.width_mm or template.default_width_mm,
            depth_mm=body.depth_mm or template.default_depth_mm,
            height_mm=body.height_mm or template.default_height_mm,
            rotation_deg=body.rotation_deg,
        )
        session.add(placement)
        session.commit()
        session.refresh(placement)
        return _placement_read(placement)

    @app.patch(
        "/api/placements/{placement_id}",
        response_model=PlacementRead,
        dependencies=[Depends(require_editor_token)],
    )
    def update_placement(
        placement_id: str,
        body: PlacementUpdate,
        session: Session = Depends(get_session),
    ):
        placement = session.get(EquipmentPlacement, placement_id)
        if placement is None:
            raise HTTPException(status_code=404, detail="设备坐标不存在")
        if placement.version != body.version:
            raise HTTPException(status_code=409, detail="设备已被其他操作更新，请刷新后重试")
        changes = body.model_dump(exclude_unset=True)
        changes.pop("version", None)
        positional_fields = {
            "x_mm",
            "y_mm",
            "z_mm",
            "width_mm",
            "depth_mm",
            "height_mm",
            "rotation_deg",
        }
        if placement.is_locked and positional_fields.intersection(changes):
            raise HTTPException(status_code=409, detail="设备已确认并锁定，不能移动、旋转或修改尺寸")
        if changes.get("is_locked") is True:
            changes["is_confirmed"] = True
        if (
            changes.get("is_confirmed") is False
            and placement.is_locked
            and changes.get("is_locked") is not False
        ):
            raise HTTPException(status_code=422, detail="解除确认前必须先显式解除设备锁定")
        for field, value in changes.items():
            setattr(placement, field, value)
        placement.version += 1
        session.commit()
        session.refresh(placement)
        return _placement_read(placement)

    @app.delete(
        "/api/placements/{placement_id}",
        status_code=204,
        dependencies=[Depends(require_editor_token)],
    )
    def delete_placement(placement_id: str, session: Session = Depends(get_session)):
        placement = session.get(EquipmentPlacement, placement_id)
        if placement is None:
            raise HTTPException(status_code=404, detail="设备坐标不存在")
        if placement.is_locked:
            raise HTTPException(status_code=409, detail="设备已锁定，必须先解除锁定才能移除")
        session.delete(placement)
        session.commit()

    @app.post(
        "/api/layouts/{layout_id}/racks",
        response_model=RackRead,
        dependencies=[Depends(require_editor_token)],
    )
    def create_rack(
        layout_id: str,
        body: RackCreate,
        session: Session = Depends(get_session),
    ):
        layout = session.get(Layout, layout_id)
        if layout is None:
            raise HTTPException(status_code=404, detail="布局不存在")
        rack = RackPlacement(layout_id=layout_id, **body.model_dump(), status="candidate")
        session.add(rack)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="当前布局中的货架编号已存在") from error
        session.refresh(rack)
        return _rack_read(rack)

    @app.patch(
        "/api/racks/{rack_id}",
        response_model=RackRead,
        dependencies=[Depends(require_editor_token)],
    )
    def update_rack(
        rack_id: str,
        body: RackUpdate,
        session: Session = Depends(get_session),
    ):
        rack = session.get(RackPlacement, rack_id)
        if rack is None:
            raise HTTPException(status_code=404, detail="货架不存在")
        if rack.version != body.version:
            raise HTTPException(status_code=409, detail="货架已被其他操作更新，请刷新后重试")
        changes = body.model_dump(exclude_unset=True)
        changes.pop("version", None)
        locked_fields = {
            "x_mm",
            "y_mm",
            "width_mm",
            "depth_mm",
            "height_mm",
            "levels",
            "bays",
            "access_side",
            "min_aisle_width_mm",
            "rotation_deg",
        }
        if rack.is_locked and locked_fields.intersection(changes):
            raise HTTPException(status_code=409, detail="货架已确认并锁定，不能移动或修改参数")
        for field, value in changes.items():
            setattr(rack, field, value)
        if locked_fields.intersection(changes):
            rack.status = "candidate"
        rack.version += 1
        session.commit()
        session.refresh(rack)
        return _rack_read(rack)

    @app.post(
        "/api/racks/{rack_id}/confirm",
        response_model=RackRead,
        dependencies=[Depends(require_editor_token)],
    )
    def confirm_rack(
        rack_id: str,
        version: int,
        session: Session = Depends(get_session),
    ):
        rack = session.get(RackPlacement, rack_id)
        if rack is None:
            raise HTTPException(status_code=404, detail="货架不存在")
        if rack.version != version:
            raise HTTPException(status_code=409, detail="货架版本已变化，请刷新后重试")
        rack.status = "confirmed"
        rack.is_locked = True
        rack.version += 1
        session.commit()
        session.refresh(rack)
        return _rack_read(rack)

    @app.delete(
        "/api/racks/{rack_id}",
        status_code=204,
        dependencies=[Depends(require_editor_token)],
    )
    def delete_rack(rack_id: str, session: Session = Depends(get_session)):
        rack = session.get(RackPlacement, rack_id)
        if rack is None:
            raise HTTPException(status_code=404, detail="货架不存在")
        if rack.is_locked:
            raise HTTPException(status_code=409, detail="货架已锁定，必须先解除锁定才能移除")
        session.delete(rack)
        session.commit()

    @app.post(
        "/api/layouts/{layout_id}/features",
        response_model=FeatureRead,
        dependencies=[Depends(require_editor_token)],
    )
    def create_feature(
        layout_id: str,
        body: FeatureCreate,
        session: Session = Depends(get_session),
    ):
        layout = session.get(Layout, layout_id)
        if layout is None:
            raise HTTPException(status_code=404, detail="布局不存在")
        payload = body.model_dump()
        points = payload.pop("points")
        feature = LayoutFeature(
            layout_id=layout_id,
            points_json=points,
            area_mm2=feature_area_mm2(body.feature_kind, points, body.width_mm),
            status="candidate",
            **payload,
        )
        session.add(feature)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail="当前布局中的区域或通道编号已存在") from error
        session.refresh(feature)
        return _feature_read(feature)

    @app.patch(
        "/api/features/{feature_id}",
        response_model=FeatureRead,
        dependencies=[Depends(require_editor_token)],
    )
    def update_feature(
        feature_id: str,
        body: FeatureUpdate,
        session: Session = Depends(get_session),
    ):
        feature = session.get(LayoutFeature, feature_id)
        if feature is None:
            raise HTTPException(status_code=404, detail="布局区域不存在")
        if feature.version != body.version:
            raise HTTPException(status_code=409, detail="布局区域已被其他操作更新，请刷新后重试")
        changes = body.model_dump(exclude_unset=True)
        changes.pop("version", None)
        if "points" in changes:
            feature.points_json = changes.pop("points")
        for field, value in changes.items():
            setattr(feature, field, value)
        if feature.feature_kind != "zone" and (
            feature.storage_mode != "floor" or feature.elevation_mm != 0
        ):
            raise HTTPException(status_code=422, detail="只有堆放区域可以设置垂直存放层级")
        if (
            feature.feature_kind == "zone"
            and feature.storage_mode != "floor"
            and feature.elevation_mm <= 0
        ):
            raise HTTPException(status_code=422, detail="货架上层或架空区域必须设置大于 0 的离地高度")
        feature.area_mm2 = feature_area_mm2(
            feature.feature_kind, feature.points_json, feature.width_mm
        )
        feature.status = "candidate"
        feature.version += 1
        session.commit()
        session.refresh(feature)
        return _feature_read(feature)

    @app.post(
        "/api/features/{feature_id}/confirm",
        response_model=FeatureRead,
        dependencies=[Depends(require_editor_token)],
    )
    def confirm_feature(
        feature_id: str,
        version: int,
        session: Session = Depends(get_session),
    ):
        feature = session.get(LayoutFeature, feature_id)
        if feature is None:
            raise HTTPException(status_code=404, detail="布局区域不存在")
        if feature.version != version:
            raise HTTPException(status_code=409, detail="布局区域版本已变化，请刷新后重试")
        feature.status = "confirmed"
        feature.version += 1
        session.commit()
        session.refresh(feature)
        return _feature_read(feature)

    @app.delete(
        "/api/features/{feature_id}",
        status_code=204,
        dependencies=[Depends(require_editor_token)],
    )
    def delete_feature(feature_id: str, session: Session = Depends(get_session)):
        feature = session.get(LayoutFeature, feature_id)
        if feature is None:
            raise HTTPException(status_code=404, detail="布局区域不存在")
        session.delete(feature)
        session.commit()

    return app


app = create_app()
