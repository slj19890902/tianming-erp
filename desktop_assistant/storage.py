"""Verified archives and portable, password encrypted recovery bundles."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import zipfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

CHUNK = 1024 * 1024
MAGIC = b'TMERPBACKUP1\n'


def archive_path(path: Path) -> Path:
    """Resolve archive paths, including ZSpace virtual volumes (WinError 1005).

    These volumes support normal file I/O but not GetFinalPathNameByHandle.
    Never use the fallback through a reparse point or for other OS errors.
    """
    try:
        return path.resolve()
    except OSError as error:
        if getattr(error, 'winerror', None) != 1005:
            raise
        absolute = Path(os.path.abspath(path))
        for part in (absolute, *absolute.parents):
            try:
                attributes = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(attributes.st_mode) or getattr(attributes, 'st_file_attributes', 0) & 0x400:
                raise ValueError('NAS归档路径不能经过链接或重解析点') from error
        return absolute


def sha(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path: Path, value) -> None:
    temp = path.with_name(path.name + '.pending')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def database_info(path: Path) -> dict:
    if not path.is_file():
        raise ValueError('数据库文件不存在')
    with closing(sqlite3.connect(archive_path(path).as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('数据库完整性检查失败')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('数据库外键检查失败')
        heads = [r[0] for r in db.execute('SELECT version_num FROM alembic_version')]
        if len(heads) != 1:
            raise ValueError('数据库必须有唯一迁移版本')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'users', 'customers', 'products', 'sales_orders'}
        if not required <= tables:
            raise ValueError('缺少ERP业务表')
        counts = {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for name in sorted(tables)}
        return {'revision': heads[0], 'counts': counts}


def safe_name(name: str) -> str:
    # Windows is case-insensitive; disallow alternate streams, devices and aliases.
    value = PurePosixPath(name)
    if not name or '\\' in name or value.is_absolute() or str(value) != name:
        raise ValueError('归档路径不合法')
    for part in value.parts:
        if part in ('.', '..') or ':' in part or part.endswith((' ', '.')):
            raise ValueError('归档路径越界')
        if part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
            raise ValueError('归档设备路径不合法')
    return name


def signed_release_manifest(archive: Path, public_key: bytes) -> dict:
    with zipfile.ZipFile(archive) as zipped:
        raw = zipped.read('manifest.json')
        key = serialization.load_pem_public_key(public_key)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError('发布公钥类型错误')
        key.verify(zipped.read('manifest.sig'), raw)
        manifest = json.loads(raw)
        if manifest.get('type') != 'tianming.release.v1':
            raise ValueError('不是程序发布包')
        return manifest


def extract_verified(archive: Path, target: Path, public_key: bytes | None = None) -> dict:
    """New directory only. For release archives, verify publisher BEFORE extraction."""
    if target.exists():
        raise ValueError('解包目录必须不存在')
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        names = [safe_name(i.filename) for i in infos]
        if len({n.casefold() for n in names}) != len(names):
            raise ValueError('归档含重复路径')
        if any(stat.S_ISLNK(i.external_attr >> 16) for i in infos):
            raise ValueError('归档不得含符号链接')
        if sum(i.file_size for i in infos) > 100 * 1024**3:
            raise ValueError('恢复包超过100GB，请使用专项恢复流程')
        manifest_bytes = zipped.read('manifest.json')
        if public_key is not None:
            key = serialization.load_pem_public_key(public_key)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError('发布公钥类型错误')
            key.verify(zipped.read('manifest.sig'), manifest_bytes)
        manifest = json.loads(manifest_bytes)
        files = manifest['files']
        expected = set(files) | {'manifest.json'} | ({'manifest.sig'} if public_key is not None else set())
        if set(names) != expected:
            raise ValueError('归档文件清单不一致')
        target.mkdir(parents=True)
        for name, expected_hash in files.items():
            safe_name(name)
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(name) as source, destination.open('xb') as dest:
                while block := source.read(CHUNK):
                    dest.write(block)
            if sha(destination) != expected_hash:
                raise ValueError('文件校验失败：' + name)
        write_json(target / 'manifest.json', manifest)
        return manifest


def pack_tree(source: Path, archive: Path, metadata: dict, signer=None) -> dict:
    paths = sorted(p for p in source.rglob('*') if p.is_file())
    if any(p.is_symlink() or p.is_junction() for p in source.rglob('*')):
        raise ValueError('备份源包含链接，请先规范数据路径')
    return _pack_paths([(p, p.relative_to(source).as_posix()) for p in paths], archive, metadata, signer)


def pack_recovery(shared: Path, packages: dict, archive: Path, metadata: dict) -> dict:
    """Stream stopped local data into NAS ZIP; no loose NAS SQLite/small files."""
    items = sorted(shared.rglob('*'))
    if shared.is_symlink() or shared.is_junction() or any(p.is_symlink() or p.is_junction() for p in items):
        raise ValueError('备份源包含链接，请先规范数据路径')
    paths = [(p, 'shared/' + p.relative_to(shared).as_posix()) for p in items if p.is_file()]
    for name, path in packages.items():
        if path.is_symlink() or path.is_junction():
            raise ValueError('备份程序包不能是链接')
        paths.append((path, name))
    return _pack_paths(paths, archive, metadata)


def _pack_paths(paths, archive: Path, metadata: dict, signer=None) -> dict:
    files = {safe_name(name): sha(path) for path, name in paths}
    if len(files) != len(paths):
        raise ValueError('备份源含重复路径')
    if 'manifest.json' in files or 'manifest.sig' in files:
        raise ValueError('源目录包含保留文件名')
    manifest = {**metadata, 'files': files}
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zipped:
        for path, name in paths:
            zipped.write(path, name)
            if sha(path) != files[name]:
                raise ValueError('打包期间文件发生变化')
        zipped.writestr('manifest.json', raw)
        if signer is not None:
            zipped.writestr('manifest.sig', signer.sign(raw))
    return manifest


def _key(password: str, salt: bytes) -> bytes:
    if len(password) < 12:
        raise ValueError('恢复口令至少12个字符，请保存在电脑之外')
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=1200000).derive(password.encode('utf-8'))


def encrypt_file(source: Path, destination: Path, password: str) -> None:
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    cipher = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce)).encryptor()
    cipher.authenticate_additional_data(header)
    with source.open('rb') as src, destination.open('xb') as dst:
        dst.write(header)
        while block := src.read(CHUNK):
            dst.write(cipher.update(block))
        dst.write(cipher.finalize())
        dst.write(cipher.tag)


def decrypt_file(source: Path, destination: Path, password: str) -> None:
    with source.open('rb') as src:
        header = src.read(len(MAGIC) + 28)
        if not header.startswith(MAGIC) or len(header) != len(MAGIC) + 28:
            raise ValueError('不是ERP完整恢复包')
        salt, nonce = header[len(MAGIC):len(MAGIC)+16], header[-12:]
        remaining = source.stat().st_size - len(header) - 16
        if remaining < 0:
            raise ValueError('恢复包不完整')
        src.seek(-16, 2)
        tag = src.read(16)
        src.seek(len(header))
        cipher = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce, tag)).decryptor()
        cipher.authenticate_additional_data(header)
        try:
            with destination.open('xb') as dst:
                while remaining:
                    block = src.read(min(CHUNK, remaining))
                    if not block:
                        raise ValueError('恢复包截断')
                    remaining -= len(block)
                    dst.write(cipher.update(block))
                dst.write(cipher.finalize())
        except Exception:
            destination.unlink(missing_ok=True)
            raise ValueError('恢复口令错误或备份已损坏') from None
