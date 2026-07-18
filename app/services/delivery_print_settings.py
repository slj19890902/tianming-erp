"""Persistent, non-business settings for the delivery-note print layout.

The settings intentionally live beside the selected ERP database instead of in a
business table.  A printer/paper calibration must not require a schema migration
or alter delivery, order, or financial records.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from app.core.config import load_settings


DEFAULT_DELIVERY_PRINT_SETTINGS = {
    "paper_width_mm": 241.0,
    "paper_height_mm": 139.5,
}
MIN_PAPER_WIDTH_MM = 100.0
MAX_PAPER_WIDTH_MM = 400.0
MIN_PAPER_HEIGHT_MM = 80.0
MAX_PAPER_HEIGHT_MM = 400.0
_SETTINGS_FILENAME = "delivery_print_settings.json"


def delivery_print_settings_path() -> Path:
    """Return the per-database print settings file location."""
    return load_settings().database_path.parent / _SETTINGS_FILENAME


def _validated_dimension(value: Any, *, minimum: float, maximum: float, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}必须是数字") from error
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError(f"{label}必须在 {minimum:g}-{maximum:g}mm 之间")
    rounded = round(number, 2)
    if not math.isclose(number, rounded, abs_tol=1e-9):
        raise ValueError(f"{label}最多保留两位小数")
    return rounded


def normalize_delivery_print_settings(payload: dict[str, Any]) -> dict[str, float]:
    """Validate the stable public API contract for print paper dimensions."""
    return {
        "paper_width_mm": _validated_dimension(
            payload.get("paper_width_mm"),
            minimum=MIN_PAPER_WIDTH_MM,
            maximum=MAX_PAPER_WIDTH_MM,
            label="送货单打印纸宽",
        ),
        "paper_height_mm": _validated_dimension(
            payload.get("paper_height_mm"),
            minimum=MIN_PAPER_HEIGHT_MM,
            maximum=MAX_PAPER_HEIGHT_MM,
            label="送货单打印纸高",
        ),
    }


def get_delivery_print_settings() -> dict[str, float]:
    """Read settings without creating or modifying a file on first access."""
    path = delivery_print_settings_path()
    if not path.is_file():
        return dict(DEFAULT_DELIVERY_PRINT_SETTINGS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置不是对象")
        return normalize_delivery_print_settings(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        # Printer calibration must never stop printing because a hand-edited
        # local config file is invalid.  Falling back is safe and deterministic.
        return dict(DEFAULT_DELIVERY_PRINT_SETTINGS)


def save_delivery_print_settings(payload: dict[str, Any]) -> dict[str, float]:
    """Atomically persist validated settings beside the selected database."""
    settings = normalize_delivery_print_settings(payload)
    path = delivery_print_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(settings, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return settings
