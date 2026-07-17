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
from app.core.password_policy import normalize_username, password_policy_issues
from app.core.security import hash_password
from app.models.user import USER_ROLES


VALID_ROLES = tuple(sorted(USER_ROLES))


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
        choices=(
            "list-users",
            "create-user",
            "reset-password",
            "disable-user",
            "enable-user",
            "set-role",
        ),
    )
    parser.add_argument("--sqlite-path", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--role", choices=VALID_ROLES, default=None)
    parser.add_argument("--actor", default=None)
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


def validate_password(password: str, username: str) -> None:
    issues = password_policy_issues(password, username=username)
    if issues:
        raise SystemExit(f"password does not meet policy: {'; '.join(issues)}")


def validate_username(username: str | None) -> str:
    if username is None:
        raise SystemExit("--username is required")
    try:
        return normalize_username(username)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def require_apply_actor(
    connection: sqlite3.Connection,
    args: argparse.Namespace,
) -> tuple[int, str, str, int] | None:
    if not args.apply:
        return None
    actor_value = getattr(args, "actor", None)
    if not actor_value:
        raise SystemExit("--actor is required with --apply")
    try:
        actor = normalize_username(actor_value)
    except ValueError as error:
        raise SystemExit(f"invalid --actor: {error}") from error
    row = connection.execute(
        """
        SELECT id, username, role, is_active
        FROM users
        WHERE username = ?
        """,
        (actor,),
    ).fetchone()
    if row is None or row[2] != "admin" or not row[3]:
        raise SystemExit("--actor must identify an active admin")
    return row


def ensure_active_admin_remains(
    connection: sqlite3.Connection,
    *,
    current_role: str,
    current_is_active: bool,
    next_role: str,
    next_is_active: bool,
) -> None:
    active_admin_count = connection.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
    ).fetchone()[0]
    removes_active_admin = (
        current_role == "admin"
        and current_is_active
        and (next_role != "admin" or not next_is_active)
    )
    if removes_active_admin and active_admin_count <= 1:
        raise SystemExit("operation refused: at least one active admin must remain")


def create_local_backup(db_path: Path) -> str:
    result = backup_to_nas(
        source_path=db_path,
        backup_dir=db_path.parent / "backups",
        filename_suffix="_before_account_rbac_hardening",
    )
    if (
        result.integrity_check.lower() != "ok"
        or not result.path.is_file()
        or result.size <= 0
        or len(result.sha256) != 64
    ):
        raise RuntimeError("verified maintenance backup was not created")
    return str(result.path)


def begin_audited_apply(
    connection: sqlite3.Connection,
    args: argparse.Namespace,
    db_path: Path,
) -> tuple[tuple[int, str, str, int], str]:
    backup_path = create_local_backup(db_path)
    connection.execute("BEGIN IMMEDIATE")
    actor = require_apply_actor(connection, args)
    if actor is None:
        raise RuntimeError("audited apply transaction requires --apply")
    return actor, backup_path


def write_maintenance_log(
    connection: sqlite3.Connection,
    *,
    actor: tuple[int, str, str, int],
    action: str,
    target_id: int,
    target_username: str,
    details: dict[str, object],
) -> None:
    connection.execute(
        """
        INSERT INTO operation_logs (
            user_id, action, resource, details, username, role,
            entity_type, entity_id, description
        ) VALUES (?, ?, 'User', ?, ?, ?, 'user', ?, ?)
        """,
        (
            actor[0],
            action,
            json.dumps(
                {"target_username": target_username, **details},
                ensure_ascii=False,
                sort_keys=True,
            ),
            actor[1],
            actor[2],
            target_id,
            "受控账号维护",
        ),
    )


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
    if args.username is None or not args.role:
        raise SystemExit("--username and --role are required")
    username = validate_username(args.username)
    password = prompt_or_env_password(args.password_env)
    validate_password(password, username)
    with sqlite3.connect(db_path) as connection:
        require_apply_actor(connection, args)
        row = connection.execute(
            "SELECT username, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row:
            return [
                ActionResult(
                    action="create-user",
                    username=username,
                    role=row[1],
                    applied=args.apply,
                    changed=False,
                    message="user already exists",
                )
            ]
        backup_path = None
        if args.apply:
            password_hash = hash_password(password)
            actor, backup_path = begin_audited_apply(connection, args, db_path)
            if connection.execute(
                "SELECT 1 FROM users WHERE username = ?",
                (username,),
            ).fetchone():
                raise SystemExit("user already exists")
            cursor = connection.execute(
                """
                INSERT INTO users
                (username, password_hash, role, real_name, display_name, is_active, must_change_password)
                VALUES (?, ?, ?, ?, ?, 1, 1)
                """,
                (
                    username,
                    password_hash,
                    args.role,
                    username,
                    username,
                ),
            )
            write_maintenance_log(
                connection,
                actor=actor,
                action="MAINT_CREATE_USER",
                target_id=int(cursor.lastrowid),
                target_username=username,
                details={"role": args.role},
            )
            connection.commit()
    return [
        ActionResult(
            action="create-user",
            username=username,
            role=args.role,
            applied=args.apply,
            changed=args.apply,
            message="created" if args.apply else "dry-run create",
            backup_path=backup_path if args.apply else None,
        )
    ]


def reset_password_cmd(args: argparse.Namespace, db_path: Path) -> list[ActionResult]:
    username = validate_username(args.username)
    password = prompt_or_env_password(args.password_env)
    validate_password(password, username)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id, username, role FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            raise SystemExit("user not found")
        require_apply_actor(connection, args)
        backup_path = None
        if args.apply:
            password_hash = hash_password(password)
            actor, backup_path = begin_audited_apply(connection, args, db_path)
            row = connection.execute(
                "SELECT id, username, role FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if not row:
                raise SystemExit("user not found")
            connection.execute(
                """
                UPDATE users
                SET password_hash = ?,
                    must_change_password = 1,
                    auth_version = auth_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                """,
                (password_hash, username),
            )
            write_maintenance_log(
                connection,
                actor=actor,
                action="MAINT_RESET_PASSWORD",
                target_id=row[0],
                target_username=username,
                details={},
            )
            connection.commit()
    return [
        ActionResult(
            action="reset-password",
            username=username,
            role=row[2],
            applied=args.apply,
            changed=args.apply,
            message="password reset" if args.apply else "dry-run reset",
            backup_path=backup_path if args.apply else None,
        )
    ]


def disable_user_cmd(args: argparse.Namespace, db_path: Path) -> list[ActionResult]:
    username = validate_username(args.username)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id, username, role, is_active FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            raise SystemExit("user not found")
        require_apply_actor(connection, args)
        ensure_active_admin_remains(
            connection,
            current_role=row[2],
            current_is_active=bool(row[3]),
            next_role=row[2],
            next_is_active=False,
        )
        if not row[3]:
            return [
                ActionResult(
                    action="disable-user",
                    username=username,
                    role=row[2],
                    applied=args.apply,
                    changed=False,
                    message="already disabled",
                )
            ]
        backup_path = None
        if args.apply:
            actor, backup_path = begin_audited_apply(connection, args, db_path)
            row = connection.execute(
                "SELECT id, username, role, is_active FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if not row:
                raise SystemExit("user not found")
            ensure_active_admin_remains(
                connection,
                current_role=row[2],
                current_is_active=bool(row[3]),
                next_role=row[2],
                next_is_active=False,
            )
            if not row[3]:
                connection.rollback()
                return [
                    ActionResult(
                        action="disable-user",
                        username=username,
                        role=row[2],
                        applied=True,
                        changed=False,
                        message="already disabled",
                        backup_path=backup_path,
                    )
                ]
            connection.execute(
                """
                UPDATE users
                SET is_active = 0,
                    auth_version = auth_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                """,
                (username,),
            )
            write_maintenance_log(
                connection,
                actor=actor,
                action="MAINT_DISABLE_USER",
                target_id=row[0],
                target_username=username,
                details={"previous_role": row[2]},
            )
            connection.commit()
    return [
        ActionResult(
            action="disable-user",
            username=username,
            role=row[2],
            applied=args.apply,
            changed=args.apply,
            message="disabled" if args.apply else "dry-run disable",
            backup_path=backup_path if args.apply else None,
        )
    ]


def enable_user_cmd(args: argparse.Namespace, db_path: Path) -> list[ActionResult]:
    username = validate_username(args.username)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id, username, role, is_active FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            raise SystemExit("user not found")
        require_apply_actor(connection, args)
        if row[3]:
            return [
                ActionResult(
                    action="enable-user",
                    username=username,
                    role=row[2],
                    applied=args.apply,
                    changed=False,
                    message="already enabled",
                )
            ]
        backup_path = None
        if args.apply:
            actor, backup_path = begin_audited_apply(connection, args, db_path)
            row = connection.execute(
                "SELECT id, username, role, is_active FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if not row:
                raise SystemExit("user not found")
            if row[3]:
                connection.rollback()
                return [
                    ActionResult(
                        action="enable-user",
                        username=username,
                        role=row[2],
                        applied=True,
                        changed=False,
                        message="already enabled",
                        backup_path=backup_path,
                    )
                ]
            connection.execute(
                """
                UPDATE users
                SET is_active = 1,
                    auth_version = auth_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                """,
                (username,),
            )
            write_maintenance_log(
                connection,
                actor=actor,
                action="MAINT_ENABLE_USER",
                target_id=row[0],
                target_username=username,
                details={"role": row[2]},
            )
            connection.commit()
    return [
        ActionResult(
            action="enable-user",
            username=username,
            role=row[2],
            applied=args.apply,
            changed=args.apply,
            message="enabled" if args.apply else "dry-run enable",
            backup_path=backup_path if args.apply else None,
        )
    ]


def set_role_cmd(args: argparse.Namespace, db_path: Path) -> list[ActionResult]:
    if args.username is None or not args.role:
        raise SystemExit("--username and --role are required")
    username = validate_username(args.username)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT id, username, role, is_active FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            raise SystemExit("user not found")
        require_apply_actor(connection, args)
        ensure_active_admin_remains(
            connection,
            current_role=row[2],
            current_is_active=bool(row[3]),
            next_role=args.role,
            next_is_active=bool(row[3]),
        )
        if row[2] == args.role:
            return [
                ActionResult(
                    action="set-role",
                    username=username,
                    role=row[2],
                    applied=args.apply,
                    changed=False,
                    message="role unchanged",
                )
            ]
        previous_role = row[2]
        backup_path = None
        if args.apply:
            actor, backup_path = begin_audited_apply(connection, args, db_path)
            row = connection.execute(
                "SELECT id, username, role, is_active FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if not row:
                raise SystemExit("user not found")
            ensure_active_admin_remains(
                connection,
                current_role=row[2],
                current_is_active=bool(row[3]),
                next_role=args.role,
                next_is_active=bool(row[3]),
            )
            if row[2] == args.role:
                connection.rollback()
                return [
                    ActionResult(
                        action="set-role",
                        username=username,
                        role=row[2],
                        applied=True,
                        changed=False,
                        message="role unchanged",
                        backup_path=backup_path,
                    )
                ]
            previous_role = row[2]
            connection.execute(
                """
                UPDATE users
                SET role = ?,
                    auth_version = auth_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE username = ?
                """,
                (args.role, username),
            )
            write_maintenance_log(
                connection,
                actor=actor,
                action="MAINT_SET_ROLE",
                target_id=row[0],
                target_username=username,
                details={
                    "previous_role": previous_role,
                    "new_role": args.role,
                },
            )
            connection.commit()
    return [
        ActionResult(
            action="set-role",
            username=username,
            role=args.role,
            applied=args.apply,
            changed=args.apply,
            message="role changed" if args.apply else "dry-run role change",
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
        "enable-user": lambda: enable_user_cmd(args, db_path),
        "set-role": lambda: set_role_cmd(args, db_path),
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
