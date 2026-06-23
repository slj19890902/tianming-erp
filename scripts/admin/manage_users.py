from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import load_settings
from app.core.database import backup_to_nas
from app.core.security import hash_password


VALID_ROLES = ("admin", "finance", "sales", "workshop")


@dataclass
class ActionResult:
    action: str
    username: str
    role: str | None
    applied: bool
    changed: bool
    message: str
    backup_path: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage ERP users safely.")
    parser.add_argument(
        "command",
        choices=("list-users", "create-user", "reset-password", "disable-user"),
    )
    parser.add_argument("--sqlite-path", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--role", choices=VALID_ROLES, default=None)
    parser.add_argument("--password-env", default=None)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def resolve_db(path_arg: str | None) -> Path:
    current = load_settings()
    return Path(path_arg).resolve() if path_arg else current.database_path.resolve()


def fetch_users(connection: sqlite3.Connection) -> list[dict]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT id, username, role, is_active, must_change_password, password_hash
        FROM users
        ORDER BY id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def ensure_modern_hash(password_hash: str) -> bool:
    return password_hash.startswith(("$2a$", "$2b$", "$2y$"))


def prompt_or_env_password(env_name: str | None) -> str:
    if env_name:
        value = os.environ.get(env_name, "")
        if not value:
            raise SystemExit(f"password env {env_name} is empty")
        return value
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise SystemExit("password confirmation mismatch")
    return first


def create_local_backup(db_path: Path) -> str:
    result = backup_to_nas(
        source_path=db_path,
        backup_dir=db_path.parent / "backups",
        filename_suffix="_before_account_rbac_hardening",
    )
    return str(result.path)


def list_users_cmd(db_path: Path) -> list[ActionResult]:
    with sqlite3.connect(db_path) as connection:
        users = fetch_users(connection)
    return [
        ActionResult(
            action="list-users",
            username=user["username"],
            role=user["role"],
            applied=False,
            changed=False,
            message="modern-hash" if ensure_modern_hash(user["password_hash"]) else "legacy-hash",
        )
        for user in users
    ]


def create_user_cmd(args: argparse.Namespace, db_path: Path) -> list[ActionResult]:
    if not args.username or not args.role:
        raise SystemExit("--username and --role are required")
    password = prompt_or_env_password(args.password_env)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT username, role FROM users WHERE username = ?",
            (args.username,),
        ).fetchone()
        if row:
            return [
                ActionResult(
                    action="create-user",
                    username=args.username,
                    role=row[1],
                    applied=args.apply,
                    changed=False,
                    message="user already exists",
                )
            ]
        backup_path = create_local_backup(db_path) if args.apply else None
        if args.apply:
            connection.execute(
                """
                INSERT INTO users
                (username, password_hash, role, real_name, display_name, is_active, must_change_password)
                VALUES (?, ?, ?, ?, ?, 1, 1)
                """,
                (
                    args.username,
                    hash_password(password),
                    args.role,
                    args.username,
                    args.username,
                ),
            )
            connection.commit()
    return [
        ActionResult(
            action="create-user",
            username=args.username,
            role=args.role,
            applied=args.apply,
            changed=args.apply,
            message="created" if args.apply else "dry-run create",
            backup_path=backup_path if args.apply else None,
        )
    ]


def reset_password_cmd(args: argparse.Namespace, db_path: Path) -> list[ActionResult]:
    if not args.username:
        raise SystemExit("--username is required")
    password = prompt_or_env_password(args.password_env)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT username, role FROM users WHERE username = ?",
            (args.username,),
        ).fetchone()
        if not row:
            raise SystemExit("user not found")
        backup_path = create_local_backup(db_path) if args.apply else None
        if args.apply:
            connection.execute(
                """
                UPDATE users
                SET password_hash = ?, must_change_password = 1, updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                """,
                (hash_password(password), args.username),
            )
            connection.commit()
    return [
        ActionResult(
            action="reset-password",
            username=args.username,
            role=row[1],
            applied=args.apply,
            changed=args.apply,
            message="password reset" if args.apply else "dry-run reset",
            backup_path=backup_path if args.apply else None,
        )
    ]


def disable_user_cmd(args: argparse.Namespace, db_path: Path) -> list[ActionResult]:
    if not args.username:
        raise SystemExit("--username is required")
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT username, role, is_active FROM users WHERE username = ?",
            (args.username,),
        ).fetchone()
        if not row:
            raise SystemExit("user not found")
        if not row[2]:
            return [
                ActionResult(
                    action="disable-user",
                    username=args.username,
                    role=row[1],
                    applied=args.apply,
                    changed=False,
                    message="already disabled",
                )
            ]
        backup_path = create_local_backup(db_path) if args.apply else None
        if args.apply:
            connection.execute(
                "UPDATE users SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
                (args.username,),
            )
            connection.commit()
    return [
        ActionResult(
            action="disable-user",
            username=args.username,
            role=row[1],
            applied=args.apply,
            changed=args.apply,
            message="disabled" if args.apply else "dry-run disable",
            backup_path=backup_path if args.apply else None,
        )
    ]


def main() -> int:
    args = parse_args()
    db_path = resolve_db(args.sqlite_path)
    command_map = {
        "list-users": lambda: list_users_cmd(db_path),
        "create-user": lambda: create_user_cmd(args, db_path),
        "reset-password": lambda: reset_password_cmd(args, db_path),
        "disable-user": lambda: disable_user_cmd(args, db_path),
    }
    results = command_map[args.command]()
    with sqlite3.connect(db_path) as connection:
        users = [
            {
                "username": row["username"],
                "role": row["role"],
                "is_active": row["is_active"],
                "must_change_password": row["must_change_password"],
                "modern_hash": ensure_modern_hash(row["password_hash"]),
                "legacy_sha256_123456": hashlib.sha256(b"123456").hexdigest() == row["password_hash"],
            }
            for row in fetch_users(connection)
        ]
    print(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "database": str(db_path),
                "results": [asdict(result) for result in results],
                "users": users,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
