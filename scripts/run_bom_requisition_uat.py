"""Run the full ERP on a loopback-only anonymous BOM UAT database."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import types

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--port", type=int, default=18094)
    args = parser.parse_args()
    database = Path(args.database).resolve()
    formal = Path(
        r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3"
    ).resolve()
    if database == formal:
        raise SystemExit("拒绝使用工厂正式数据库")
    if not database.is_file():
        raise SystemExit(f"UAT 数据库不存在：{database}")

    # The baseline imports optional Tianhua OCR code eagerly. This UAT does not
    # exercise that module; keep it unavailable instead of installing packages
    # or changing the workstation.
    try:
        import cv2  # noqa: F401
    except ModuleNotFoundError:
        sys.modules["cv2"] = types.ModuleType("cv2")

    os.environ["ERP_DATABASE_PATH"] = str(database)
    os.environ["ERP_ENVIRONMENT"] = "development"
    os.environ["ERP_BIND_HOST"] = "127.0.0.1"
    os.environ["ERP_PORT"] = str(args.port)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        app_dir=str(PROJECT_ROOT),
        host="127.0.0.1",
        port=args.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
