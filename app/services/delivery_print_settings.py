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
    "printer_model": "EPSON SK820",
    "orientation_mode": "driver_managed",
    "paper_width_mm": 241.0,
    "paper_height_mm": 139.5,
    "printable_width_mm": 200.0,
    "content_width_mm": 188.0,
    "offset_x_mm": 0.0,
    "offset_y_mm": 0.0,
}
MIN_PAPER_WIDTH_MM = 100.0
MAX_PAPER_WIDTH_MM = 400.0
MIN_PAPER_HEIGHT_MM = 80.0
MAX_PAPER_HEIGHT_MM = 400.0
_SETTINGS_FILENAME = "delivery_print_settings.json"


def delivery_print_settings_path() -> Path:
    """Return the per-database print settings file location."""
    configured = os.getenv("ERP_DELIVERY_PRINT_SETTINGS_PATH", "").strip()
    if configured:
        return Path(configured).resolve(strict=False)
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


def _validated_printer_model(value: Any) -> str:
    if value is None:
        return str(DEFAULT_DELIVERY_PRINT_SETTINGS["printer_model"])
    if not isinstance(value, str):
        raise ValueError("目标打印机型号必须是文字")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 80
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ValueError("目标打印机型号必须为 1～80 个可见字符")
    return normalized


def normalize_delivery_print_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the shared ERP print profile used by every client computer."""
    orientation_mode = payload.get(
        "orientation_mode",
        DEFAULT_DELIVERY_PRINT_SETTINGS["orientation_mode"],
    )
    if orientation_mode != "driver_managed":
        raise ValueError("送货单打印方向必须由实际打印电脑的驱动管理")
    settings = {
        "printer_model": _validated_printer_model(payload.get("printer_model")),
        "orientation_mode": orientation_mode,
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
    # Paper includes tractor margins; old explicitly calibrated profiles retain
    # their width until the administrator selects a printable span.
    printable_default = (settings["paper_width_mm"] if "content_width_mm" in payload
                         else min(200.0, settings["paper_width_mm"]))
    settings["printable_width_mm"] = _validated_dimension(
        payload.get("printable_width_mm", printable_default), minimum=100,
        maximum=settings["paper_width_mm"], label="可打印区域宽度",
    )
    settings["content_width_mm"] = _validated_dimension(
        payload.get("content_width_mm", min(188.0, settings["printable_width_mm"] - 12)),
        minimum=60, maximum=settings["printable_width_mm"] - 12, label="正文安全宽度",
    )
    for key in ("offset_x_mm", "offset_y_mm"):
        settings[key] = _validated_dimension(payload.get(key, 0), minimum=-20, maximum=20, label="打印偏移")
    if abs(settings["offset_x_mm"]) > (settings["printable_width_mm"] - settings["content_width_mm"]) / 2 - 3:
        raise ValueError("横向偏移使正文超出纸张安全区")
    if abs(settings["offset_y_mm"]) > 2:
        raise ValueError("上下偏移必须在 -2～2mm 内，避免页脚超出纸张")
    return settings


def get_delivery_print_settings() -> dict[str, Any]:
    """Read settings without creating or modifying a file on first access."""
    path = delivery_print_settings_path()
    if not path.is_file():
        return dict(DEFAULT_DELIVERY_PRINT_SETTINGS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置不是对象")
        return normalize_delivery_print_settings({
            **{k: v for k, v in DEFAULT_DELIVERY_PRINT_SETTINGS.items() if k not in ("content_width_mm", "printable_width_mm")}, **raw
        })
    except (OSError, json.JSONDecodeError, ValueError):
        # Printer calibration must never stop printing because a hand-edited
        # local config file is invalid.  Falling back is safe and deterministic.
        return dict(DEFAULT_DELIVERY_PRINT_SETTINGS)


def save_delivery_print_settings(payload: dict[str, Any]) -> dict[str, Any]:
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
