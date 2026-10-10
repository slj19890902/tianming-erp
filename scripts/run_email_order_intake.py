from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_settings
from app.core.database import SessionLocal
from app.services.email_order_intake import EmailIntakeBusyError, EmailIntakeError, poll_mailbox_once


def main() -> int:
    try:
        settings = load_settings()
    except (OSError, RuntimeError, ValueError) as error:
        print(
            json.dumps(
                {"ok": False, "message": f"邮箱收单配置无效：{error}"},
                ensure_ascii=False,
            )
        )
        return 2
    if not settings.email_intake_enabled:
        print(json.dumps({"ok": False, "message": "邮箱自动收单尚未启用"}, ensure_ascii=False))
        return 2
    try:
        with SessionLocal() as session:
            result = poll_mailbox_once(session, settings=settings)
    except EmailIntakeBusyError as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False))
        return 3
    except EmailIntakeError as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "mailbox_key": settings.email_intake_mailbox_key,
                **result,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
