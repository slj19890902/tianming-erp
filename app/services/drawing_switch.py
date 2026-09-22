"""Stop new V2 edits/adoption while retaining access to released history."""
import os
from fastapi import HTTPException


def drawing_v2_enabled() -> bool:
    return os.getenv("ERP_DRAWING_V2_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}


def require_drawing_v2_write() -> None:
    if not drawing_v2_enabled():
        raise HTTPException(503, "图纸V2新建、编辑及采用已停用；历史发布版仍可查看和重印")
