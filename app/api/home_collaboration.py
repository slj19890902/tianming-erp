"""Small authenticated home tools. Uploads are immutable private originals."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from app.api.deps import get_current_user, get_db
from app.core.time_contract import utc_now_naive
from app.models.user import User
from app.models.audit import OperationLog
from app.models.home_collaboration import HomeMemo, SharedFile, SharedFileVersion, HomeCollaborationMutation
from app.services.secure_uploads import (UploadPolicy, UploadValidationError, read_validated_upload,
    store_private_upload, resolve_stored_reference)

router = APIRouter()
FILE_POLICY = UploadPolicy(frozenset({".xlsx", ".xls", ".docx", ".doc", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}),
    20 * 1024 * 1024, frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword", "application/pdf", "image/png", "image/jpeg", "image/webp"}), "共享")


class Mutation(BaseModel):
    request_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    expected_version: int = Field(ge=0)


class MemoWrite(Mutation):
    id: UUID
    text: str = Field(min_length=1, max_length=600)
    completed: bool = False
    deleted: bool = False

    @field_validator("text")
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("请填写一行待办事项")
        return value


class FileState(Mutation):
    deleted: bool


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _request_hash(action, payload):
    return hashlib.sha256(_json([action, payload]).encode()).hexdigest()


def _replay(db, user, key, digest):
    row = db.scalar(select(HomeCollaborationMutation).where(
        HomeCollaborationMutation.actor_id == user.id, HomeCollaborationMutation.request_key == key))
    if row is None:
        return None
    if row.request_hash != digest:
        raise HTTPException(409, "操作编号已用于其他内容，请刷新后重试")
    return json.loads(row.response_json)


def _commit(db, user, key, digest, result, action, object_id):
    db.add(HomeCollaborationMutation(actor_id=user.id, request_key=key, request_hash=digest,
        response_json=_json(result), created_at=utc_now_naive()))
    # Personal memo content is deliberately absent from the general audit log.
    db.add(OperationLog(user_id=user.id, action=action, resource="home_collaboration",
        details=_json({"id": object_id, "version": result["version"]})))
    db.commit()
    return result


def _conflict(db, error):
    db.rollback()
    raise HTTPException(409, "内容刚被更新或服务正忙，原输入已保留；请重新加载后重试") from error


def memo_dict(row):
    return {"id": row.id, "text": row.text, "completed": row.completed, "deleted": row.deleted,
            "version": row.version, "updated_at": row.updated_at.isoformat() + "Z"}


@router.get("/memos")
def memos(response: Response, completed: bool = False, page: int = Query(1, ge=1),
          limit: int = Query(4, ge=1, le=30), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers["Cache-Control"] = "private, no-store"
    scope = (HomeMemo.owner_id == user.id, HomeMemo.deleted.is_(False))
    rows = db.scalars(select(HomeMemo).where(*scope, HomeMemo.completed == completed)
        .order_by(HomeMemo.updated_at.desc(), HomeMemo.id).offset((page-1)*limit).limit(limit)).all()
    counts = dict(db.execute(select(HomeMemo.completed, func.count()).where(*scope).group_by(HomeMemo.completed)).all())
    return {"items": [memo_dict(row) for row in rows], "total": counts.get(completed, 0),
            "active_count": counts.get(False, 0), "completed_count": counts.get(True, 0)}


@router.post("/memos")
def write_memo(payload: MemoWrite, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    values = payload.model_dump(mode="json")
    digest = _request_hash("memo", values)
    try:
        replay = _replay(db, user, payload.request_key, digest)
        if replay is not None:
            return replay
        row = db.get(HomeMemo, str(payload.id))
        now = utc_now_naive()
        if row is None:
            if payload.expected_version != 0 or payload.deleted:
                raise HTTPException(404, "待办不存在，请刷新")
            row = HomeMemo(id=str(payload.id), owner_id=user.id, text=payload.text,
                completed=payload.completed, deleted=False, version=1, created_at=now, updated_at=now)
            db.add(row)
            db.flush()
        else:
            if row.owner_id != user.id:
                raise HTTPException(404, "待办不存在")
            changed = db.execute(update(HomeMemo).where(HomeMemo.id == row.id,
                HomeMemo.owner_id == user.id, HomeMemo.version == payload.expected_version)
                .values(text=payload.text, completed=payload.completed, deleted=payload.deleted,
                        version=HomeMemo.version+1, updated_at=now))
            if changed.rowcount != 1:
                raise HTTPException(409, "待办已在其他窗口修改，原输入已保留；请重新加载后核对")
            db.refresh(row)
        return _commit(db, user, payload.request_key, digest, memo_dict(row), "memo_save", row.id)
    except (IntegrityError, OperationalError) as error:
        _conflict(db, error)
    except Exception:
        db.rollback()
        raise


def _can_manage(user, row):
    return row.owner_id == user.id or user.role in {"admin", "boss"}


def _file(db, user, file_id, *, allow_deleted=False):
    row = db.get(SharedFile, str(file_id))
    if row is None or (row.deleted and not (allow_deleted and _can_manage(user, row))):
        raise HTTPException(404, "文件不存在或已移入回收站")
    return row


def file_dict(row, content, actor, user):
    return {"id": row.id, "name": row.name, "version": row.version, "content_version": row.content_version,
        "deleted": row.deleted, "can_manage": _can_manage(user, row), "updated_at": row.updated_at.isoformat()+"Z",
        "updated_by": actor.display_name or actor.real_name or actor.username,
        "size": content.size, "content_type": content.content_type}


def _file_result(db, row, user):
    content = db.scalar(select(SharedFileVersion).where(SharedFileVersion.file_id == row.id,
        SharedFileVersion.version == row.content_version))
    return file_dict(row, content, db.get(User, row.updated_by), user)


@router.get("/files")
def files(response: Response, q: str = Query("", max_length=100), deleted: bool = False,
          page: int = Query(1, ge=1), limit: int = Query(4, ge=1, le=30),
          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers["Cache-Control"] = "private, no-store"
    scope = [SharedFile.deleted == deleted]
    if deleted and user.role not in {"admin", "boss"}:
        scope.append(SharedFile.owner_id == user.id)
    if q.strip():
        scope.append(SharedFile.name.contains(q.strip(), autoescape=True))
    total = db.scalar(select(func.count()).select_from(SharedFile).where(*scope))
    rows = db.execute(select(SharedFile, SharedFileVersion, User)
        .join(SharedFileVersion, (SharedFileVersion.file_id == SharedFile.id) &
              (SharedFileVersion.version == SharedFile.content_version))
        .join(User, User.id == SharedFile.updated_by).where(*scope)
        .order_by(SharedFile.updated_at.desc(), SharedFile.id).offset((page-1)*limit).limit(limit)).all()
    return {"items": [file_dict(a, b, c, user) for a,b,c in rows], "total": total}


@router.post("/files")
async def upload_file(file: UploadFile = File(...), request_key: str = Form(..., pattern=r"^[A-Za-z0-9_-]{8,80}$"),
    file_id: UUID | None = Form(None), expected_version: int = Form(0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        validated = await read_validated_upload(file, FILE_POLICY)
    except UploadValidationError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        await file.close()
    digest = _request_hash("upload", {"file_id": str(file_id) if file_id else None,
        "expected_version": expected_version, "name": validated.original_filename,
        "sha256": hashlib.sha256(validated.content).hexdigest()})
    try:
        replay = _replay(db, user, request_key, digest)
        if replay is not None:
            return replay
        now = utc_now_naive()
        if file_id:
            row = _file(db, user, file_id)
            changed = db.execute(update(SharedFile).where(SharedFile.id == row.id,
                SharedFile.version == expected_version, SharedFile.deleted.is_(False))
                .values(version=SharedFile.version+1, content_version=SharedFile.content_version+1,
                    name=validated.original_filename, updated_by=user.id, updated_at=now))
            if changed.rowcount != 1:
                raise HTTPException(409, "文件已有新版本，请下载最新文件核对；本次没有覆盖")
            db.refresh(row)
        else:
            if expected_version != 0:
                raise HTTPException(422, "新增文件版本应为0")
            row = SharedFile(id=str(uuid4()), name=validated.original_filename, owner_id=user.id,
                updated_by=user.id, version=1, content_version=1, deleted=False, created_at=now, updated_at=now)
            db.add(row)
            db.flush()
        stored = store_private_upload(validated, category="shared_files")
        db.add(SharedFileVersion(file_id=row.id, version=row.content_version, reference=stored.reference,
            original_name=stored.original_filename, content_type=stored.content_type, size=stored.size,
            sha256=stored.sha256, actor_id=user.id, created_at=now))
        db.flush()
        return _commit(db, user, request_key, digest, _file_result(db, row, user), "file_upload", row.id)
    except (IntegrityError, OperationalError) as error:
        _conflict(db, error)
    except Exception:
        db.rollback()
        # A failed/ambiguous commit must never unlink a possibly referenced original.
        # Unreferenced immutable uploads remain protected for reviewed reconciliation.
        raise


@router.post("/files/{file_id}/state")
def file_state(file_id: UUID, payload: FileState, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    digest = _request_hash("file_state", [str(file_id), payload.model_dump()])
    try:
        row = _file(db, user, file_id, allow_deleted=True)
        if not _can_manage(user, row):
            raise HTTPException(403, "仅上传者或管理员可以删除、恢复文件")
        replay = _replay(db, user, payload.request_key, digest)
        if replay is not None:
            return replay
        changed = db.execute(update(SharedFile).where(SharedFile.id == row.id, SharedFile.version == payload.expected_version)
            .values(deleted=payload.deleted, version=SharedFile.version+1, updated_by=user.id, updated_at=utc_now_naive()))
        if changed.rowcount != 1:
            raise HTTPException(409, "文件已改变，请刷新后核对")
        db.refresh(row)
        return _commit(db, user, payload.request_key, digest, _file_result(db, row, user), "file_state", row.id)
    except (IntegrityError, OperationalError) as error:
        _conflict(db, error)
    except Exception:
        db.rollback()
        raise


@router.get("/files/{file_id}/versions")
def file_versions(file_id: UUID, response: Response, page: int = Query(1, ge=1),
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    response.headers["Cache-Control"] = "private, no-store"
    row = _file(db, user, file_id, allow_deleted=True)
    rows = db.execute(select(SharedFileVersion, User).join(User, User.id == SharedFileVersion.actor_id)
        .where(SharedFileVersion.file_id == row.id).order_by(SharedFileVersion.version.desc())
        .offset((page-1)*20).limit(20)).all()
    return {"total": row.content_version, "items": [{"version": v.version, "name": v.original_name,
        "size": v.size, "created_at": v.created_at.isoformat()+"Z", "updated_by": u.display_name or u.real_name or u.username}
        for v,u in rows]}


@router.get("/files/{file_id}/content")
def file_content(file_id: UUID, version: int | None = Query(None, ge=1), preview: bool = False,
    db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = _file(db, user, file_id, allow_deleted=True)
    content = db.scalar(select(SharedFileVersion).where(SharedFileVersion.file_id == row.id,
        SharedFileVersion.version == (version or row.content_version)))
    if not content:
        raise HTTPException(404, "该文件版本不存在")
    if not content.reference.startswith("private:shared_files/"):
        raise HTTPException(409, "文件存储依据异常，请管理员核对")
    try:
        path = resolve_stored_reference(content.reference)
        if not path.is_file() or path.stat().st_size != content.size:
            raise OSError("missing or changed original")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != content.sha256:
            raise OSError("digest mismatch")
    except (OSError, ValueError) as error:
        raise HTTPException(409, "文件原件缺失或校验不符，请管理员从备份核对") from error
    inline = preview and content.content_type in {"application/pdf", "image/png", "image/jpeg", "image/webp"}
    return FileResponse(path, filename=content.original_name, media_type=content.content_type,
        content_disposition_type="inline" if inline else "attachment", headers={
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'"})
