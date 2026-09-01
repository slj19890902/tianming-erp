"""Shared safety defaults for the test suite.

The suite contains legacy tests, subprocesses, and module-level database
engines. Establish disposable paths before collection, reject every checkout
database as a test target, and fingerprint this checkout for accidental writes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TEST_ROOT: Path | None = None
_PROTECTED_BEFORE: dict[Path, tuple[bool, int | None, int | None, str | None]] = {}


def _install_processing_settings_test_seed() -> None:
    """Make metadata-only test databases match the migrated production shape."""
    from sqlalchemy import event

    from app.models.processing_cost import ProcessingCostSettings

    table = ProcessingCostSettings.__table__
    if event.contains(table, "after_create", _seed_processing_settings_for_tests):
        return
    event.listen(table, "after_create", _seed_processing_settings_for_tests)


def _seed_processing_settings_for_tests(_target, connection, **_kwargs) -> None:
    from app.models.processing_cost import ProcessingCostSettings

    connection.execute(ProcessingCostSettings.__table__.insert().values(id=1))


def _primary_worktree_root() -> Path:
    """Resolve the main worktree without invoking Git or mutating either tree."""
    dot_git = PROJECT_ROOT / ".git"
    if dot_git.is_dir():
        return PROJECT_ROOT
    if not dot_git.is_file():
        return PROJECT_ROOT
    marker = "gitdir:"
    content = dot_git.read_text(encoding="utf-8").strip()
    if not content.lower().startswith(marker):
        return PROJECT_ROOT
    git_dir = Path(content[len(marker) :].strip())
    if not git_dir.is_absolute():
        git_dir = (PROJECT_ROOT / git_dir).resolve()
    if git_dir.parent.name.lower() == "worktrees":
        return git_dir.parent.parent.parent.resolve()
    return git_dir.parent.resolve()


PROTECTED_DATABASES = tuple(
    dict.fromkeys(
        path.resolve()
        for path in (
            PROJECT_ROOT / "data" / "carton_erp.sqlite3",
            _primary_worktree_root() / "data" / "carton_erp.sqlite3",
        )
    )
)
# The main-worktree database may be served by a live process and can change for
# reasons unrelated to pytest. Reject it as ERP_DATABASE_PATH, but fingerprint
# only this integration checkout so an external write cannot be misattributed.
FINGERPRINT_DATABASES = (
    (PROJECT_ROOT / "data" / "carton_erp.sqlite3").resolve(),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(
    path: Path,
) -> tuple[bool, int | None, int | None, str | None]:
    if not path.is_file():
        return False, None, None, None
    stat = path.stat()
    return True, stat.st_size, stat.st_mtime_ns, _sha256(path)


def pytest_configure(config: pytest.Config) -> None:
    """Point implicit application globals at disposable locations before collection."""
    global _TEST_ROOT
    del config

    if os.getenv("ERP_TEST_ALLOW_PRODUCTION_DATABASE") == "1":
        raise pytest.UsageError(
            "Production database tests are not permitted in the shared baseline."
        )

    configured_database = os.getenv("ERP_DATABASE_PATH")
    if configured_database:
        database_path = Path(configured_database).expanduser().resolve()
        if database_path in PROTECTED_DATABASES:
            raise pytest.UsageError(
                "ERP_DATABASE_PATH points at a protected checkout database; "
                "use a disposable test database instead."
            )
    else:
        database_path = None

    _TEST_ROOT = Path(tempfile.mkdtemp(prefix="tm-erp-pytest-"))
    os.environ["ERP_DATABASE_PATH"] = str(
        database_path or (_TEST_ROOT / "carton_erp.sqlite3")
    )
    os.environ["ERP_BACKUP_DIR"] = str(_TEST_ROOT / "backups")
    os.environ["ERP_SECRET_KEY_FILE"] = str(_TEST_ROOT / "session_secret.key")
    os.environ["ERP_SECRET_KEY"] = "pytest-isolated-only"
    os.environ["ERP_ENVIRONMENT"] = "test"
    _install_processing_settings_test_seed()

    _PROTECTED_BEFORE.clear()
    _PROTECTED_BEFORE.update(
        {path: _fingerprint(path) for path in FINGERPRINT_DATABASES}
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run if pytest creates or mutates a protected checkout DB."""
    del exitstatus
    changed: list[Path] = []
    for path, before in _PROTECTED_BEFORE.items():
        if _fingerprint(path) != before:
            changed.append(path)
    if changed:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        for path in changed:
            print(f"\nERROR: pytest changed a protected database: {path}")

    if _TEST_ROOT is not None:
        shutil.rmtree(_TEST_ROOT, ignore_errors=True)


@pytest.fixture
def isolated_database_path(tmp_path: Path) -> Path:
    """Return a per-test SQLite path for tests that need a real file."""
    return tmp_path / "erp-test.sqlite3"


@pytest.fixture(scope="session")
def current_alembic_head() -> str:
    """Return the repository's unique Alembic head for release-chain tests."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected one Alembic head, got {heads}"
    return heads[0]


@pytest.fixture(scope="session")
def seed_supplier_master():
    """Return an explicit helper for legacy tests that require supplier facts.

    This is intentionally not autouse: tests that verify missing suppliers must
    continue to start without hidden master data.
    """
    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity

    def seed(session_factory, name: str, business_code: str) -> int:
        normalized_name = normalize_supplier_identity(name)
        normalized_code = business_code.strip().upper()
        with session_factory() as db:
            supplier = (
                db.query(Supplier)
                .filter(Supplier.normalized_name == normalized_name)
                .one_or_none()
            )
            if supplier is None:
                supplier = Supplier(
                    standard_name=name,
                    normalized_name=normalized_name,
                    display_name=name,
                    business_code=business_code,
                    normalized_business_code=normalized_code,
                    is_active=True,
                    version=1,
                )
                db.add(supplier)
                db.commit()
            else:
                assert supplier.is_active is True
            return supplier.id

    return seed


@pytest.fixture
def isolated_engine(isolated_database_path: Path):
    """Create and dispose a SQLite engine bound to the per-test file."""
    from app.core.database import create_sqlite_engine

    engine = create_sqlite_engine(isolated_database_path)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def reject_checkout_database_writes(request: pytest.FixtureRequest):
    """Identify the exact test that creates or mutates a checkout database."""
    before = {path: _fingerprint(path) for path in FINGERPRINT_DATABASES}
    yield
    changed = [
        path
        for path, fingerprint in before.items()
        if _fingerprint(path) != fingerprint
    ]
    if changed:
        rendered = ", ".join(str(path) for path in changed)
        pytest.fail(
            f"{request.node.nodeid} changed a protected checkout database: "
            f"{rendered}"
        )


@pytest.fixture(scope="module")
def isolated_subprocess_database(tmp_path_factory: pytest.TempPathFactory):
    """Give legacy subprocess API tests a migrated-shape disposable database."""
    from sqlalchemy.orm import Session

    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    database_path = (
        tmp_path_factory.mktemp("subprocess-database") / "carton_erp.sqlite3"
    )
    previous_database_path = os.environ.get("ERP_DATABASE_PATH")
    os.environ["ERP_DATABASE_PATH"] = str(database_path)

    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            User(
                username="pytest-subprocess-admin",
                password_hash=hash_password("PytestOnly123!"),
                role="admin",
                real_name="测试管理员",
                display_name="测试管理员",
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()

    try:
        yield database_path
    finally:
        engine.dispose()
        if previous_database_path is None:
            os.environ.pop("ERP_DATABASE_PATH", None)
        else:
            os.environ["ERP_DATABASE_PATH"] = previous_database_path
