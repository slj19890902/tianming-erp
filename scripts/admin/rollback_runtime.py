"""P0-5B phase 1: prepare signed, isolated old-code compatibility evidence.

This command deliberately has no checkout, service-stop, database-restore, or
runtime-switch operation.  It may write only below an explicitly supplied
empty isolated output directory.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__:
    from scripts.admin import release_erp
    from scripts.admin.release_state_common import (
        ReleaseStateError,
        assert_ancestor,
        code_revision_at,
        runtime_config_fingerprint,
        sha256_file,
        sign_evidence,
        sqlite_logical_fingerprint_via_copy,
        verify_evidence,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import release_erp  # type: ignore[no-redef]
    from release_state_common import (  # type: ignore[no-redef]
        ReleaseStateError,
        assert_ancestor,
        code_revision_at,
        runtime_config_fingerprint,
        sha256_file,
        sign_evidence,
        sqlite_logical_fingerprint_via_copy,
        verify_evidence,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POINTER_PURPOSE = "release-pointer-v2"
PLAN_PURPOSE = "release-plan-v3"
RUNTIME_MANIFEST_PURPOSE = "rollback-runtime-manifest-v1"
COMPATIBILITY_REPORT_PURPOSE = "rollback-compatibility-v1"
DEPENDENCY_MANIFESTS = ("requirements.txt", "pyproject.toml", "poetry.lock")
CONTROLLER_FILES = (
    "scripts/admin/release_erp.py",
    "scripts/admin/release_state_common.py",
    "scripts/admin/rollback_runtime.py",
    "scripts/admin/rollback_runtime.ps1",
)
SAFE_INHERITED_ENVIRONMENT = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)


class CompatibilityError(RuntimeError):
    """The isolated compatibility evidence cannot be safely prepared."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise CompatibilityError(f"{label}不存在：{resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CompatibilityError(f"{label}无法读取：{error}") from error
    if not isinstance(payload, dict):
        raise CompatibilityError(f"{label}格式错误")
    return payload


def _verify_release_evidence(pointer_path: Path, project_root: Path) -> tuple[dict[str, Any], dict[str, Any], Path]:
    pointer = _read_json(pointer_path, label="最近发布指针")
    try:
        verify_evidence(pointer, project_root=project_root, purpose=POINTER_PURPOSE)
    except ReleaseStateError as error:
        raise CompatibilityError(str(error)) from error
    if int(pointer.get("schema_version") or 0) != 2:
        raise CompatibilityError("最近发布指针版本不受支持")
    plan_path = Path(str(pointer.get("release_plan_path") or "")).resolve()
    expected_hash = str(pointer.get("release_plan_sha256") or "")
    if not expected_hash or not plan_path.is_file() or sha256_file(plan_path) != expected_hash:
        raise CompatibilityError("发布报告与最近发布指针哈希不一致")
    plan = _read_json(plan_path, label="发布报告")
    try:
        verify_evidence(plan, project_root=project_root, purpose=PLAN_PURPOSE)
    except ReleaseStateError as error:
        raise CompatibilityError(str(error)) from error
    if int(plan.get("schema_version") or 0) != 3 or plan.get("status") != "completed":
        raise CompatibilityError("发布报告不是已完成的 P0-5A 签名证据")
    return pointer, plan, plan_path


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _require_isolated_output_root(output_root: Path, project_root: Path) -> Path:
    root = output_root.resolve()
    allowed_base = (project_root.resolve().parent / "tm-rollback-runtimes").resolve()
    if _within(root, project_root):
        raise CompatibilityError("隔离输出目录不得位于正式 checkout 内")
    if root == allowed_base or not _within(root, allowed_base):
        raise CompatibilityError(
            f"隔离输出目录必须位于固定目录内：{allowed_base}"
        )
    if root == Path(root.anchor):
        raise CompatibilityError("隔离输出目录不得为驱动器根目录")
    for candidate in (allowed_base, root):
        if not candidate.exists():
            continue
        attributes = int(
            getattr(candidate.stat(), "st_file_attributes", 0)
        )
        if candidate.is_symlink() or (
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise CompatibilityError("隔离输出目录不得使用链接或 reparse point")
    if root.exists() and any(root.iterdir()):
        raise CompatibilityError(f"隔离输出目录必须不存在或为空：{root}")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _git(project_root: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(["git", *args], cwd=project_root, check=False, capture_output=True, text=text, encoding="utf-8" if text else None)
    if result.returncode:
        detail = (result.stderr if text else result.stderr.decode("utf-8", "replace")).strip()
        raise CompatibilityError(f"Git 检查失败：{detail}")
    return result.stdout


def _assert_formal_checkout_unchanged(project_root: Path, before: tuple[str, str, str]) -> None:
    after = (
        str(_git(project_root, "branch", "--show-current")).strip(),
        str(_git(project_root, "rev-parse", "HEAD")).strip(),
        str(_git(project_root, "status", "--porcelain")).strip(),
    )
    if after != before:
        raise CompatibilityError("兼容演练期间正式 checkout 状态发生变化，证据无效")


def _normalized_manifest_bytes(payload: bytes, *, label: str) -> bytes:
    try:
        return (
            payload.decode("utf-8-sig")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .encode("utf-8")
        )
    except UnicodeDecodeError as error:
        raise CompatibilityError(f"依赖清单不是 UTF-8：{label}") from error


def _dependency_manifest_hashes(project_root: Path, previous_sha: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for relative in DEPENDENCY_MANIFESTS:
        current = project_root / relative
        exists_now = current.is_file()
        exists_old = subprocess.run(["git", "cat-file", "-e", f"{previous_sha}:{relative}"], cwd=project_root, capture_output=True).returncode == 0
        if exists_now != exists_old:
            raise CompatibilityError(f"旧版本与当前依赖清单存在性不一致：{relative}")
        if not exists_now:
            result[relative] = {"exists": False}
            continue
        old = _git(project_root, "show", f"{previous_sha}:{relative}", text=False)
        assert isinstance(old, bytes)
        old_normalized = _normalized_manifest_bytes(old, label=relative)
        current_normalized = _normalized_manifest_bytes(
            current.read_bytes(),
            label=relative,
        )
        old_hash = __import__("hashlib").sha256(old_normalized).hexdigest()
        current_hash = __import__("hashlib").sha256(current_normalized).hexdigest()
        if old_hash != current_hash:
            raise CompatibilityError(f"旧版本与当前依赖清单不一致：{relative}")
        result[relative] = {
            "exists": True,
            "normalized_sha256": current_hash,
            "size": len(current_normalized),
        }
    return result


def _archive_runtime(project_root: Path, previous_sha: str, runtime_dir: Path) -> None:
    if runtime_dir.exists():
        raise CompatibilityError(f"旧版本运行目录已存在，拒绝覆盖：{runtime_dir}")
    archive = _git(project_root, "archive", "--format=tar", previous_sha, text=False)
    assert isinstance(archive, bytes)
    runtime_dir.mkdir(parents=True, exist_ok=False)
    archive_path = runtime_dir.parent / ".rollback-source.tar"
    try:
        archive_path.write_bytes(archive)
        with tarfile.open(archive_path) as bundle:
            for member in bundle.getmembers():
                candidate = (runtime_dir / member.name).resolve()
                if not _within(candidate, runtime_dir) or member.issym() or member.islnk():
                    raise CompatibilityError("Git archive 包含不安全路径或链接")
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Python 3.14 will, by default, filter extracted tar archives",
                    category=DeprecationWarning,
                )
                bundle.extractall(runtime_dir)
    finally:
        if archive_path.exists():
            archive_path.unlink()


def _file_manifest(runtime_dir: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for path in sorted(runtime_dir.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(runtime_dir).as_posix(), "size": path.stat().st_size, "sha256": sha256_file(path)})
    digest = __import__("hashlib").sha256(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"algorithm": "git-archive-files-v1", "sha256": digest, "file_count": len(files), "files": files}


def _controller_manifest(project_root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for relative in CONTROLLER_FILES:
        path = project_root / relative
        if not path.is_file():
            raise CompatibilityError(f"回退控制文件不存在：{relative}")
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    digest = __import__("hashlib").sha256(
        json.dumps(
            files,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "algorithm": "rollback-controller-files-v1",
        "sha256": digest,
        "file_count": len(files),
        "files": files,
    }


def _isolated_environment(
    database: Path,
    bind_host: str,
    port: int,
    secret_key: str,
) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in SAFE_INHERITED_ENVIRONMENT
    }
    env.update(
        {
            "ERP_ENVIRONMENT": "test",
            "ERP_DATABASE_PATH": str(database),
            "ERP_BIND_HOST": bind_host,
            "ERP_PORT": str(port),
            "ERP_WORKERS": "1",
            "ERP_HEALTH_URL": f"http://{bind_host}:{port}/api/health",
            "ERP_BROWSER_URL": f"http://{bind_host}:{port}/",
            "ERP_SECRET_KEY": secret_key,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _assert_port_available(bind_host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        if probe.connect_ex((bind_host, port)) == 0:
            raise CompatibilityError(f"隔离端口已被占用：{bind_host}:{port}")


def _listener_identity(
    *,
    launcher_pid: int,
    bind_host: str,
    port: int,
) -> dict[str, Any]:
    if os.name != "nt":
        return {
            "launcher_pid": launcher_pid,
            "listener_pid": launcher_pid,
            "listener_ancestry": [launcher_pid],
            "verification": "same-process-non-windows",
        }
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise CompatibilityError("无法核对隔离端口所属进程")
    command = (
        "$ErrorActionPreference='Stop';"
        f"$c=Get-NetTCPConnection -State Listen -LocalPort {port} | "
        f"Where-Object{{$_.LocalAddress -eq '{bind_host}'}} | "
        "Select-Object -First 1;"
        "if(-not $c){throw 'listener missing'};"
        "$listener=[int]$c.OwningProcess;$current=$listener;$ancestry=@();"
        "for($i=0;$i -lt 32 -and $current -gt 0;$i++){"
        "$ancestry += $current;"
        f"if($current -eq {launcher_pid}){{break}};"
        "$p=Get-CimInstance Win32_Process -Filter "
        "(\"ProcessId = \" + $current);"
        "if(-not $p){break};$current=[int]$p.ParentProcessId};"
        "[pscustomobject]@{listener_pid=$listener;ancestry=$ancestry}"
        "|ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise CompatibilityError(
            f"无法核对隔离端口所属进程：{(result.stderr or result.stdout).strip()}"
        )
    try:
        payload = json.loads(result.stdout)
        listener_pid = int(payload["listener_pid"])
        ancestry = [int(value) for value in payload["ancestry"]]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise CompatibilityError("隔离端口进程证据格式错误") from error
    if launcher_pid not in ancestry:
        raise CompatibilityError("健康响应不是由本次隔离旧程序进程提供")
    return {
        "launcher_pid": launcher_pid,
        "listener_pid": listener_pid,
        "listener_ancestry": ancestry,
        "verification": "windows-tcp-owner-ancestry",
    }


def _stop_isolated_process_tree(
    process: subprocess.Popen[str],
    *,
    bind_host: str,
    port: int,
) -> None:
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                [
                    "taskkill.exe",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=4)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex((bind_host, port)) != 0:
                return
        time.sleep(0.1)
    raise CompatibilityError("隔离旧程序进程树或端口未完全退出")


def _run_old_alembic_current(
    python: Path,
    runtime_dir: Path,
    database: Path,
    bind_host: str,
    port: int,
    secret_key: str,
) -> str:
    env = _isolated_environment(database, bind_host, port, secret_key)
    result = subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-m", "alembic", "current"], cwd=runtime_dir, env=env, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise CompatibilityError(f"旧代码 Alembic 只读 current 检查失败：{(result.stderr or result.stdout).strip()}")
    return (result.stdout or result.stderr).strip()


def _start_health_check(
    python: Path,
    runtime_dir: Path,
    database: Path,
    bind_host: str,
    port: int,
    timeout: int,
    secret_key: str,
) -> dict[str, Any]:
    health_url = f"http://{bind_host}:{port}/api/health"
    env = _isolated_environment(database, bind_host, port, secret_key)
    _assert_port_available(bind_host, port)
    log_path = runtime_dir.parent / "old_runtime.log"
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    health: dict[str, Any] | None = None
    process: subprocess.Popen[str]
    with log_path.open("x", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [
                str(python),
                "-I",
                "-B",
                "-X",
                "utf8",
                "-m",
                "uvicorn",
                "app.main:app",
                "--app-dir",
                str(runtime_dir),
                "--host",
                bind_host,
                "--port",
                str(port),
                "--workers",
                "1",
            ],
            cwd=runtime_dir,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            creationflags=creation_flags,
        )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log_handle.flush()
                    detail = log_path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    ).strip()
                    raise CompatibilityError(
                        f"旧代码隔离启动提前退出：{detail}"
                    )
                try:
                    with urllib.request.urlopen(
                        health_url,
                        timeout=1,
                    ) as response:
                        body = response.read().decode("utf-8")
                        if (
                            response.status == 200
                            and json.loads(body) == {"ok": True}
                        ):
                            identity = _listener_identity(
                                launcher_pid=process.pid,
                                bind_host=bind_host,
                                port=port,
                            )
                            health = {
                                "health_url": health_url,
                                "status_code": 200,
                                "body": {"ok": True},
                                **identity,
                            }
                            break
                except (
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    urllib.error.URLError,
                    TimeoutError,
                ):
                    time.sleep(0.25)
            if health is None:
                raise CompatibilityError("旧代码隔离健康检查超时")
        finally:
            _stop_isolated_process_tree(
                process,
                bind_host=bind_host,
                port=port,
            )
    health["process_log_path"] = str(log_path)
    health["process_log_sha256"] = sha256_file(log_path)
    return health


def _write_signed(path: Path, payload: dict[str, Any], *, project_root: Path, purpose: str) -> dict[str, Any]:
    if path.exists():
        raise CompatibilityError(f"证据文件已存在，拒绝覆盖：{path}")
    signed = sign_evidence(payload, project_root=project_root, purpose=purpose)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(signed, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return signed


def _python_environment(project_root: Path, python_path: Path) -> dict[str, Any]:
    resolved = python_path.resolve()
    if resolved != Path(sys.executable).resolve():
        raise CompatibilityError("隔离启动必须使用执行本工具的同一 Python 环境")
    current_runtime = runtime_config_fingerprint(project_root)
    return {
        "python_executable": str(resolved),
        "python_version": sys.version,
        "installed_distribution_count": current_runtime[
            "installed_distribution_count"
        ],
        "installed_distributions_sha256": current_runtime[
            "installed_distributions_sha256"
        ],
    }


def prepare_compatibility(*, pointer_path: Path, database: Path, output_root: Path, report_path: Path, port: int, bind_host: str = "127.0.0.1", python_path: Path = Path(sys.executable), project_root: Path = PROJECT_ROOT, timeout_seconds: int = 20) -> dict[str, Any]:
    """Prepare signed old-code compatibility evidence without touching formal state."""
    if port == 8000 or not (1024 <= port <= 65535):
        raise CompatibilityError("隔离端口必须有效且不得为正式端口 8000")
    if bind_host != "127.0.0.1":
        raise CompatibilityError("隔离演练只允许 127.0.0.1 loopback 监听")
    project_root = project_root.resolve()
    output_root = _require_isolated_output_root(output_root, project_root)
    before_checkout = (
        str(_git(project_root, "branch", "--show-current")).strip(),
        str(_git(project_root, "rev-parse", "HEAD")).strip(),
        str(_git(project_root, "status", "--porcelain")).strip(),
    )
    report_path = report_path.resolve()
    if not _within(report_path, output_root):
        raise CompatibilityError("兼容报告必须位于隔离输出目录内")
    python_path = python_path.resolve()
    if not python_path.is_file():
        raise CompatibilityError(f"隔离启动 Python 不存在：{python_path}")
    python_environment = _python_environment(project_root, python_path)
    controller_manifest = _controller_manifest(project_root)
    pointer, plan, plan_path = _verify_release_evidence(pointer_path, project_root)
    previous_sha = str(plan.get("previous_code_sha") or "")
    release_sha = str(plan.get("code_sha") or "")
    if len(previous_sha) != 40 or previous_sha == release_sha:
        raise CompatibilityError("上一版本 SHA 必须且只能来自已签名发布报告")
    if str(pointer.get("previous_code_sha") or "") != previous_sha or str(pointer.get("code_sha") or "") != release_sha:
        raise CompatibilityError("发布指针与发布报告的旧/新 SHA 不一致")
    if before_checkout[1] != release_sha:
        raise CompatibilityError("当前 checkout 不是最近签名发布版本")
    try:
        assert_ancestor(project_root, previous_sha, release_sha)
    except ReleaseStateError as error:
        raise CompatibilityError(str(error)) from error
    previous_revision = code_revision_at(project_root, previous_sha)
    if previous_revision != str(plan.get("previous_code_revision") or ""):
        raise CompatibilityError("旧版本 Alembic head 与发布报告不一致")
    dependency_manifests = _dependency_manifest_hashes(project_root, previous_sha)
    formal_database = database.resolve()
    planned_database = Path(str((plan.get("source") or {}).get("path") or "")).resolve()
    if formal_database != planned_database:
        raise CompatibilityError("当前数据库路径与已签名发布报告不一致")
    current_snapshot = release_erp.inspect_database(formal_database)
    release_erp.assert_healthy(current_snapshot, label="当前数据库")
    release_erp.assert_business_smoke(current_snapshot, label="当前数据库")
    current_logical = sqlite_logical_fingerprint_via_copy(formal_database)
    completed_logical = ((plan.get("completed_database") or {}).get("logical_fingerprint") or {})
    completed_hash = str(completed_logical.get("sha256") or "")
    if not completed_hash:
        raise CompatibilityError("发布报告缺少发布完成时数据库逻辑指纹")
    if current_logical["sha256"] == completed_hash:
        source_database = Path(str((plan.get("backup") or {}).get("path") or "")).resolve()
        expected_backup_hash = str((plan.get("backup") or {}).get("sha256") or "")
        if not source_database.is_file() or sha256_file(source_database) != expected_backup_hash:
            raise CompatibilityError("更新前备份不存在或哈希不一致")
        mode, mode_label = "full_rollback", "完整回退兼容演练"
    elif current_snapshot["revision"] == previous_revision:
        source_database = formal_database
        mode, mode_label = "code_only", "仅代码回退兼容演练"
    else:
        raise CompatibilityError("当前数据已变化且 schema 不兼容旧代码，禁止准备仅代码回退")
    run_id = f"{previous_sha[:12]}-{datetime.now():%Y%m%d_%H%M%S}"
    run_root = output_root / run_id
    runtime_dir = run_root / "runtime"
    rehearsal_database = run_root / "database" / "compatibility.sqlite3"
    if run_root.exists():
        raise CompatibilityError(f"隔离演练目录已存在，拒绝覆盖：{run_root}")
    run_root.mkdir(parents=True)
    try:
        _archive_runtime(project_root, previous_sha, runtime_dir)
        for name, item in dependency_manifests.items():
            if not item.get("exists"):
                continue
            extracted_hash = __import__("hashlib").sha256(
                _normalized_manifest_bytes(
                    (runtime_dir / name).read_bytes(),
                    label=name,
                )
            ).hexdigest()
            if extracted_hash != item["normalized_sha256"]:
                raise CompatibilityError("解压后的旧版本依赖清单与已核对内容不一致")
        archive_manifest = _file_manifest(runtime_dir)
        source_snapshot = release_erp.create_sqlite_copy(source_database, rehearsal_database)
        release_erp.assert_healthy(source_snapshot, label="隔离演练副本")
        if source_snapshot["revision"] != previous_revision:
            raise CompatibilityError("隔离副本 revision 与旧代码 head 不一致")
        logical_before = sqlite_logical_fingerprint_via_copy(rehearsal_database)
        isolated_secret = secrets.token_urlsafe(48)
        alembic_current = _run_old_alembic_current(
            python_path,
            runtime_dir,
            rehearsal_database,
            bind_host,
            port,
            isolated_secret,
        )
        health = _start_health_check(
            python_path,
            runtime_dir,
            rehearsal_database,
            bind_host,
            port,
            timeout_seconds,
            isolated_secret,
        )
        logical_after = sqlite_logical_fingerprint_via_copy(rehearsal_database)
        if logical_before != logical_after:
            raise CompatibilityError("旧代码隔离演练改变了数据库逻辑内容，禁止生成兼容证据")
        post_snapshot = release_erp.inspect_database(rehearsal_database)
        release_erp.assert_healthy(post_snapshot, label="旧代码演练后副本")
        if post_snapshot["revision"] != previous_revision:
            raise CompatibilityError("旧代码演练后数据库 revision 发生变化")
        if _file_manifest(runtime_dir) != archive_manifest:
            raise CompatibilityError("旧代码隔离启动改变了运行目录，禁止生成兼容证据")
        formal_logical_after = sqlite_logical_fingerprint_via_copy(formal_database)
        if formal_logical_after != current_logical:
            raise CompatibilityError("兼容演练期间当前数据库发生变化，证据无效")
        _assert_formal_checkout_unchanged(project_root, before_checkout)
        manifest_path = run_root / "runtime_manifest.json"
        manifest = _write_signed(manifest_path, {"schema_version": 1, "created_at": _utc_now(), "previous_code_sha": previous_sha, "previous_code_revision": previous_revision, "runtime_dir": str(runtime_dir), "dependency_manifests": dependency_manifests, "archive_manifest": archive_manifest, "python_environment": python_environment, "controller_manifest": controller_manifest}, project_root=project_root, purpose=RUNTIME_MANIFEST_PURPOSE)
        prepared_at = datetime.now(timezone.utc)
        report = _write_signed(report_path, {"schema_version": 1, "status": "compatibility_ready", "execution_eligible": False, "evidence_nonce": secrets.token_hex(24), "prepared_at": prepared_at.isoformat(timespec="seconds"), "expires_at": (prepared_at + timedelta(hours=24)).isoformat(timespec="seconds"), "mode": mode, "mode_label": mode_label, "pointer_path": str(pointer_path.resolve()), "pointer_sha256": sha256_file(pointer_path.resolve()), "plan_path": str(plan_path), "plan_sha256": sha256_file(plan_path), "previous_code_sha": previous_sha, "release_code_sha": release_sha, "previous_code_revision": previous_revision, "formal_database": str(formal_database), "formal_database_snapshot": current_snapshot, "formal_database_logical_fingerprint": current_logical, "source_database": str(source_database), "runtime_dir": str(runtime_dir), "runtime_manifest_path": str(manifest_path), "runtime_manifest_sha256": sha256_file(manifest_path), "rehearsal_database": str(rehearsal_database), "rehearsal_before": source_snapshot, "rehearsal_after": post_snapshot, "rehearsal_logical_fingerprint_before": logical_before, "rehearsal_logical_fingerprint_after": logical_after, "runtime_environment": {"environment": "test", "bind_host": bind_host, "port": port, "database_path": str(rehearsal_database), "workers": 1, "secret_source": "ephemeral_environment", "inherited_environment_names": sorted(name for name in _isolated_environment(rehearsal_database, bind_host, port, isolated_secret) if name.upper() in SAFE_INHERITED_ENVIRONMENT)}, "alembic_current": alembic_current, "health": health}, project_root=project_root, purpose=COMPATIBILITY_REPORT_PURPOSE)
        return {"ok": True, "status": "compatibility_ready", "mode": mode, "mode_label": mode_label, "previous_code_sha": previous_sha, "release_code_sha": release_sha, "runtime_dir": str(runtime_dir), "report_path": str(report_path), "rehearsal_database": str(rehearsal_database), "health_url": health["health_url"], "runtime_manifest_path": str(manifest_path), "read_only_formal": True, "evidence_hmac": report.get("evidence_hmac")}
    except Exception:
        _assert_formal_checkout_unchanged(project_root, before_checkout)
        raise


def verify_compatibility(
    *,
    report_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Revalidate every mutable object bound by a compatibility report."""
    project_root = project_root.resolve()
    before_checkout = (
        str(_git(project_root, "branch", "--show-current")).strip(),
        str(_git(project_root, "rev-parse", "HEAD")).strip(),
        str(_git(project_root, "status", "--porcelain")).strip(),
    )
    try:
        report_path = report_path.resolve()
        report = _read_json(report_path, label="兼容报告")
        try:
            verify_evidence(
                report,
                project_root=project_root,
                purpose=COMPATIBILITY_REPORT_PURPOSE,
            )
        except ReleaseStateError as error:
            raise CompatibilityError(str(error)) from error
        if (
            int(report.get("schema_version") or 0) != 1
            or report.get("status") != "compatibility_ready"
            or report.get("execution_eligible") is not False
        ):
            raise CompatibilityError("兼容报告状态或版本不受支持")
        if not str(report.get("evidence_nonce") or ""):
            raise CompatibilityError("兼容报告缺少单次证据编号")
        try:
            expires_at = datetime.fromisoformat(
                str(report.get("expires_at") or "")
            )
        except ValueError as error:
            raise CompatibilityError("兼容报告有效期格式错误") from error
        if datetime.now(timezone.utc) >= expires_at:
            raise CompatibilityError("兼容报告已过期，必须重新隔离演练")

        pointer_path = Path(str(report.get("pointer_path") or "")).resolve()
        if (
            not pointer_path.is_file()
            or sha256_file(pointer_path) != report.get("pointer_sha256")
        ):
            raise CompatibilityError("最近发布指针已变化，兼容证据失效")
        pointer, plan, plan_path = _verify_release_evidence(
            pointer_path,
            project_root,
        )
        if (
            str(plan_path) != str(report.get("plan_path") or "")
            or sha256_file(plan_path) != report.get("plan_sha256")
        ):
            raise CompatibilityError("发布报告已变化，兼容证据失效")
        previous_sha = str(report.get("previous_code_sha") or "")
        release_sha = str(report.get("release_code_sha") or "")
        previous_revision = str(report.get("previous_code_revision") or "")
        if (
            previous_sha != str(plan.get("previous_code_sha") or "")
            or release_sha != str(plan.get("code_sha") or "")
            or previous_revision != str(plan.get("previous_code_revision") or "")
            or previous_sha != str(pointer.get("previous_code_sha") or "")
            or release_sha != str(pointer.get("code_sha") or "")
        ):
            raise CompatibilityError("兼容报告与签名发布证据不一致")
        if before_checkout[1] != release_sha:
            raise CompatibilityError("当前 checkout 不是兼容报告绑定的发布版本")
        try:
            assert_ancestor(project_root, previous_sha, release_sha)
        except ReleaseStateError as error:
            raise CompatibilityError(str(error)) from error

        evidence_root = report_path.parent
        manifest_path = Path(
            str(report.get("runtime_manifest_path") or "")
        ).resolve()
        runtime_dir = Path(str(report.get("runtime_dir") or "")).resolve()
        rehearsal_database = Path(
            str(report.get("rehearsal_database") or "")
        ).resolve()
        for label, path in (
            ("运行清单", manifest_path),
            ("旧版本目录", runtime_dir),
            ("演练数据库", rehearsal_database),
        ):
            if not _within(path, evidence_root):
                raise CompatibilityError(f"{label}逃出隔离证据目录")
        if (
            not manifest_path.is_file()
            or sha256_file(manifest_path)
            != report.get("runtime_manifest_sha256")
        ):
            raise CompatibilityError("旧版本运行清单已变化")
        manifest = _read_json(manifest_path, label="旧版本运行清单")
        try:
            verify_evidence(
                manifest,
                project_root=project_root,
                purpose=RUNTIME_MANIFEST_PURPOSE,
            )
        except ReleaseStateError as error:
            raise CompatibilityError(str(error)) from error
        if (
            int(manifest.get("schema_version") or 0) != 1
            or manifest.get("previous_code_sha") != previous_sha
            or manifest.get("previous_code_revision") != previous_revision
            or Path(str(manifest.get("runtime_dir") or "")).resolve()
            != runtime_dir
        ):
            raise CompatibilityError("旧版本运行清单与兼容报告不一致")
        if not runtime_dir.is_dir() or (
            _file_manifest(runtime_dir) != manifest.get("archive_manifest")
        ):
            raise CompatibilityError("旧版本运行文件已变化，兼容证据失效")
        if manifest.get("python_environment") != _python_environment(
            project_root,
            Path(sys.executable),
        ):
            raise CompatibilityError("Python 依赖环境已变化，兼容证据失效")
        if manifest.get("controller_manifest") != _controller_manifest(
            project_root
        ):
            raise CompatibilityError("回退控制文件已变化，兼容证据失效")

        process_log = Path(
            str((report.get("health") or {}).get("process_log_path") or "")
        ).resolve()
        if (
            not _within(process_log, evidence_root)
            or not process_log.is_file()
            or sha256_file(process_log)
            != (report.get("health") or {}).get("process_log_sha256")
        ):
            raise CompatibilityError("旧程序隔离启动日志已变化")

        rehearsal_snapshot = release_erp.inspect_database(rehearsal_database)
        release_erp.assert_healthy(rehearsal_snapshot, label="演练数据库")
        if rehearsal_snapshot["revision"] != previous_revision:
            raise CompatibilityError("演练数据库 revision 已变化")
        rehearsal_logical = sqlite_logical_fingerprint_via_copy(
            rehearsal_database
        )
        if rehearsal_logical != report.get(
            "rehearsal_logical_fingerprint_after"
        ):
            raise CompatibilityError("演练数据库内容已变化，兼容证据失效")

        formal_database = Path(
            str(report.get("formal_database") or "")
        ).resolve()
        planned_database = Path(
            str((plan.get("source") or {}).get("path") or "")
        ).resolve()
        if formal_database != planned_database:
            raise CompatibilityError("兼容报告的当前数据库路径与发布证据不一致")
        formal_snapshot = release_erp.inspect_database(formal_database)
        release_erp.assert_healthy(formal_snapshot, label="当前数据库")
        formal_logical = sqlite_logical_fingerprint_via_copy(formal_database)
        if formal_logical != report.get("formal_database_logical_fingerprint"):
            raise CompatibilityError("当前数据库内容已变化，兼容证据失效")

        return {
            "ok": True,
            "status": "compatibility_verified",
            "mode": report["mode"],
            "mode_label": report["mode_label"],
            "previous_code_sha": previous_sha,
            "release_code_sha": release_sha,
            "report_path": str(report_path),
            "runtime_dir": str(runtime_dir),
            "rehearsal_database": str(rehearsal_database),
            "read_only_formal": True,
        }
    finally:
        _assert_formal_checkout_unchanged(project_root, before_checkout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="天明 ERP P0-5B 隔离旧代码兼容演练")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-compatibility", help="只准备签名兼容证据，不执行回退")
    prepare.add_argument("--pointer", type=Path, required=True)
    prepare.add_argument("--database", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)
    prepare.add_argument("--report", type=Path, required=True)
    prepare.add_argument("--port", type=int, required=True)
    prepare.add_argument("--bind-host", default="127.0.0.1")
    prepare.add_argument("--timeout-seconds", type=int, default=20)
    verify = commands.add_parser(
        "verify-compatibility",
        help="重新验签并核对隔离兼容证据，不执行回退",
    )
    verify.add_argument("--report", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "prepare-compatibility":
            result = prepare_compatibility(pointer_path=args.pointer, database=args.database, output_root=args.output_root, report_path=args.report, port=args.port, bind_host=args.bind_host, python_path=Path(sys.executable), project_root=PROJECT_ROOT, timeout_seconds=args.timeout_seconds)
        elif args.command == "verify-compatibility":
            result = verify_compatibility(
                report_path=args.report,
                project_root=PROJECT_ROOT,
            )
        else:
            raise CompatibilityError("不支持的命令")
    except Exception as error:
        print(json.dumps({"ok": False, "status": "blocked", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
