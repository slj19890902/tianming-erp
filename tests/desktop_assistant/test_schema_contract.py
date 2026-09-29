from __future__ import annotations

from pathlib import Path
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant.schema_contract import (
    schema_contract_from_signed_release,
    schema_contract_from_sources,
)
from desktop_assistant.storage import pack_tree


def _sources(extra: str = "") -> dict[str, bytes]:
    return {
        "app/models/__init__.py": (
            "from sqlalchemy.orm import DeclarativeBase\n"
            "class Base(DeclarativeBase):\n    pass\n"
            "from app.models.fixture import Example\n"
        ).encode(),
        "app/models/fixture.py": (
            "from sqlalchemy.orm import Mapped, mapped_column\n"
            "from app.models import Base\n"
            + extra
            + "\nclass Example(Base):\n"
            "    __tablename__ = 'examples'\n"
            "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    implicit_flag: Mapped[bool]\n"
        ).encode(),
    }


def _signed_package(tmp_path: Path, sources: dict[str, bytes], *, declared: bool) -> tuple[Path, bytes]:
    tree = tmp_path / "tree"
    for relative, raw in sources.items():
        path = tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    metadata = {"type": "tianming.release.v1", "version": "fixture", "revision": "r1"}
    if declared:
        metadata["schema_contract"] = schema_contract_from_sources(sources, "r1")
    package = tmp_path / "release.zip"
    pack_tree(tree, package, metadata, key)
    return package, public


def test_current_models_have_complete_static_contract_including_implicit_columns(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    sources = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in (root / "app/models").glob("*.py")
    }

    contract = schema_contract_from_sources(sources, "ed0928ml")
    from app.models import Base
    runtime_tables = {
        table.name: sorted(column.name for column in table.columns)
        for table in Base.metadata.tables.values()
    }

    assert len(contract["tables"]) == 263
    assert contract["tables"] == {name: runtime_tables[name] for name in sorted(runtime_tables)}
    assert "tax_included" in contract["tables"]["finance_delivery_graph_cost_portions"]
    assert "tax_included" in contract["tables"]["raw_purchase_delivery_cost_portions"]
    assert "sales_order_items" in contract["tables"]
    package, public = _signed_package(tmp_path, sources, declared=True)
    _manifest, authenticated = schema_contract_from_signed_release(package, public)
    assert authenticated["tables"] == contract["tables"]


def test_signed_contract_and_legacy_fallback_use_verified_model_bytes(tmp_path: Path) -> None:
    sources = _sources()
    declared_package, public = _signed_package(tmp_path / "declared", sources, declared=True)
    legacy_package, legacy_public = _signed_package(tmp_path / "legacy", sources, declared=False)

    declared_manifest, declared = schema_contract_from_signed_release(declared_package, public)
    legacy_manifest, legacy = schema_contract_from_signed_release(legacy_package, legacy_public)

    assert declared_manifest["schema_contract"] == declared
    assert "schema_contract" not in legacy_manifest
    assert legacy == declared


def test_model_top_level_code_is_parsed_without_execution(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist.txt"
    package, public = _signed_package(
        tmp_path / "package",
        _sources(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"),
        declared=False,
    )

    _manifest, contract = schema_contract_from_signed_release(package, public)

    assert "examples" in contract["tables"]
    assert not marker.exists()


def test_static_keyword_column_name_is_the_required_database_name() -> None:
    sources = _sources()
    sources["app/models/fixture.py"] = (
        "from sqlalchemy.orm import Mapped, mapped_column\n"
        "from app.models import Base\n"
        "class Example(Base):\n"
        "    __tablename__ = 'examples'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        "    public_name: Mapped[int] = mapped_column(name='real_db_name')\n"
    ).encode()

    contract = schema_contract_from_sources(sources, "r1")

    assert contract["tables"]["examples"] == ["id", "real_db_name"]


@pytest.mark.parametrize(
    "fixture_source",
    [
        "from sqlalchemy.orm import Mapped\nfrom app.models import Base\n"
        "class Example(Base):\n    __tablename__ = make_name()\n    id: Mapped[int]\n",
        "from sqlalchemy.orm import Mapped\nfrom app.models import Base\n"
        "class Example(Base):\n    __tablename__ = 'examples'\n    id: Mapped[int] = make_column()\n",
        "from sqlalchemy.orm import Mapped\nfrom app.models import Base\n"
        "class Example(Base, UnknownMixin):\n    __tablename__ = 'examples'\n    id: Mapped[int]\n",
        "from sqlalchemy.orm import Mapped, mapped_column\nfrom app.models import Base\n"
        "class Example(Base):\n    __tablename__ = 'examples'\n"
        "    id: Mapped[int] = mapped_column(name=dynamic_name)\n",
        "from sqlalchemy.orm import Mapped, mapped_column\nfrom sqlalchemy import Column as C\n"
        "from app.models import Base\n"
        "class Example(Base):\n    __tablename__ = 'examples'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n    hidden = C()\n",
        "from sqlalchemy.orm import Mapped, mapped_column\nfrom app.models import Base as ModelBase\n"
        "class Example(ModelBase):\n    __tablename__ = 'examples'\n    id: Mapped[int]\n",
        "from sqlalchemy import Column\nfrom sqlalchemy.orm import Mapped, mapped_column\n"
        "from app.models import Base\nC = Column\n"
        "class Example(Base):\n    __tablename__ = 'examples'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n    hidden = C()\n",
        "import sqlalchemy as sa\nfrom sqlalchemy.orm import Mapped, mapped_column\n"
        "from app.models import Base\n"
        "class Example(Base):\n    __tablename__ = 'examples'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n    hidden = sa.Column()\n",
        "from sqlalchemy.orm import Mapped, mapped_column\nfrom app.models import Base\n"
        "class Example(Base):\n    __tablename__ = 'examples'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        "if enabled:\n    class Hidden(Base):\n        __tablename__ = 'hidden'\n"
        "        id: Mapped[int] = mapped_column(primary_key=True)\n",
        "from sqlalchemy.orm import Mapped, mapped_column\nfrom app.models import Base\n"
        "class Example(Base):\n    if enabled:\n        __tablename__ = 'examples'\n"
        "    id: Mapped[int] = mapped_column(primary_key=True)\n",
    ],
)
def test_unknown_model_structure_fails_closed(fixture_source: str) -> None:
    sources = _sources()
    sources["app/models/fixture.py"] = fixture_source.encode()

    with pytest.raises(ValueError, match="静态"):
        schema_contract_from_sources(sources, "r1")


def test_changed_model_bytes_fail_even_when_manifest_signature_is_unchanged(tmp_path: Path) -> None:
    package, public = _signed_package(tmp_path / "source", _sources(), declared=False)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered, "x") as target:
        for info in source.infolist():
            raw = source.read(info.filename)
            if info.filename == "app/models/fixture.py":
                raw += b"# changed after signing\n"
            target.writestr(info, raw)

    with pytest.raises(ValueError, match="模型文件校验失败"):
        schema_contract_from_signed_release(tampered, public)
