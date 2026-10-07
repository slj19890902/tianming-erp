"""Numeric sheet dimensions with unchanged integral SQLite storage and reads."""
from sqlalchemy import Integer, Numeric
from sqlalchemy.types import TypeDecorator

from app.core.sheet_dimensions import sheet_dimension_number


class SheetDimensionColumn(TypeDecorator):
    # SQLite INTEGER affinity already losslessly stores non-integral REAL values.
    # Retain its declaration to avoid rebuilding historical tables and triggers.
    # Other dialects need the explicit Numeric schema upgrade in ef1007cp.
    impl = Integer
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Integer() if dialect.name == "sqlite" else Numeric(12, 2))

    def process_bind_param(self, value, dialect):
        return None if value is None else sheet_dimension_number(value)

    def process_result_value(self, value, dialect):
        return None if value is None else sheet_dimension_number(value)
