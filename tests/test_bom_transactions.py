import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.services.bom_transactions import atomic_bom


@pytest.fixture
def session(tmp_path):
    engine = create_sqlite_engine(tmp_path / "atomic-bom-only.sqlite3")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE entries (id INTEGER PRIMARY KEY)")
    with Session(engine) as db:
        yield db
    engine.dispose()


def test_read_then_savepoint_does_not_commit_before_caller(session):
    db = session
    assert db.scalar(text("SELECT count(*) FROM entries")) == 0
    with atomic_bom(db):
        db.execute(text("INSERT INTO entries VALUES (1)"))
    db.rollback()
    assert db.scalar(text("SELECT count(*) FROM entries")) == 0


def test_caller_commit_persists_one_atomic_step(session):
    db = session
    with atomic_bom(db):
        db.execute(text("INSERT INTO entries VALUES (1)"))
    db.commit()
    assert db.scalar(text("SELECT count(*) FROM entries")) == 1


def test_failed_step_preserves_caller_work_but_not_partial_bom(session):
    db = session
    db.execute(text("INSERT INTO entries VALUES (1)"))
    with pytest.raises(RuntimeError):
        with atomic_bom(db):
            db.execute(text("INSERT INTO entries VALUES (2)"))
            raise RuntimeError("cost validation failed")
    db.commit()
    assert db.scalars(text("SELECT id FROM entries ORDER BY id")).all() == [1]


def test_nested_assembly_failure_rolls_back_all_levels(session):
    db = session
    with pytest.raises(RuntimeError):
        with atomic_bom(db):
            db.execute(text("INSERT INTO entries VALUES (1)"))
            with atomic_bom(db):
                db.execute(text("INSERT INTO entries VALUES (2)"))
            raise RuntimeError("final parent failed")
    db.commit()
    assert db.scalar(text("SELECT count(*) FROM entries")) == 0
