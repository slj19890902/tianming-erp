"""Explicit publisher initialization; builds never rotate signing identity."""
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from desktop_assistant.windows import protect, unprotect


def public_bytes(key):
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def load_key(path):
    raw = Path(path).read_bytes()
    if raw.startswith(b'-----BEGIN'):
        key = serialization.load_pem_private_key(raw, password=None)
    else:
        record = json.loads(raw)
        if record.get('type') != 'tianming.publisher.dpapi.v1':
            raise ValueError('不支持的发布身份格式')
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(unprotect(record['protected_key'])))
        public = public_bytes(key)
        if (public.decode('ascii') != record['public_key']
                or hashlib.sha256(public).hexdigest() != record['fingerprint']):
            raise ValueError('发布公私钥不一致，拒绝构建')
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('发布身份必须使用 Ed25519')
    return key


def initialize(directory):
    directory = Path(directory).resolve()
    path = directory / 'publisher.json'
    if path.exists():
        return hashlib.sha256(public_bytes(load_key(path))).hexdigest()
    return store_key(directory, Ed25519PrivateKey.generate())


def store_key(directory, key):
    directory = Path(directory).resolve()
    path = directory / 'publisher.json'
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('发布身份目录非空，拒绝替换或创建另一身份')
    directory.mkdir(parents=True, exist_ok=True)
    sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW).strip().split(',')[-1].strip('"')
    subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F',
                    '*S-1-5-18:(OI)(CI)F'], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    public = public_bytes(key)
    fingerprint = hashlib.sha256(public).hexdigest()
    record = {'type':'tianming.publisher.dpapi.v1','fingerprint':fingerprint,
              'public_key':public.decode('ascii'),
              'protected_key':protect(key.private_bytes(serialization.Encoding.Raw,
                  serialization.PrivateFormat.Raw, serialization.NoEncryption()).hex())}
    with path.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2)
    assert hashlib.sha256(public_bytes(load_key(path))).hexdigest() == fingerprint
    return fingerprint


def export_identity(identity, destination, password):
    if len(password) < 12:
        raise ValueError('恢复口令至少12位')
    key = load_key(identity)
    fingerprint = hashlib.sha256(public_bytes(key)).hexdigest()
    salt, nonce = os.urandom(16), os.urandom(12)
    derived = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 600000, 32)
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    ciphertext = AESGCM(derived).encrypt(nonce, raw, fingerprint.encode('ascii'))
    record = {'type':'tianming.publisher.backup.v1','fingerprint':fingerprint,
              'salt':base64.b64encode(salt).decode('ascii'), 'nonce':base64.b64encode(nonce).decode('ascii'),
              'ciphertext':base64.b64encode(ciphertext).decode('ascii')}
    # Exclusive creation; a previous recovery backup is never replaced.
    with Path(destination).open('x', encoding='utf-8') as stream:
        json.dump(record, stream, indent=2)
    verified = decrypt_identity(destination, password)
    if public_bytes(verified) != public_bytes(key):
        raise ValueError('恢复备份回读校验失败')
    return fingerprint


def decrypt_identity(source, password):
    path = Path(source)
    if path.stat().st_size > 8192:
        raise ValueError('发布身份备份格式错误')
    try:
        record = json.loads(path.read_bytes())
        if record['type'] != 'tianming.publisher.backup.v1':
            raise ValueError()
        salt, nonce, ciphertext = (base64.b64decode(record[name], validate=True)
                                   for name in ('salt','nonce','ciphertext'))
        if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) != 48:
            raise ValueError()
        derived = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 600000, 32)
        raw = AESGCM(derived).decrypt(nonce, ciphertext, record['fingerprint'].encode('ascii'))
        key = Ed25519PrivateKey.from_private_bytes(raw)
        if hashlib.sha256(public_bytes(key)).hexdigest() != record['fingerprint']:
            raise ValueError()
        return key
    except Exception:
        raise ValueError('恢复口令不正确或发布身份备份已损坏') from None


def restore_identity(source, destination, password):
    return store_key(destination, decrypt_identity(source, password))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--initialize', type=Path)
    actions.add_argument('--export', type=Path, help='加密备份输出文件，口令交互输入')
    actions.add_argument('--restore', type=Path, help='加密备份输入文件，口令交互输入')
    parser.add_argument('--identity', type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if args.initialize:
        print(initialize(args.initialize))
    elif args.export:
        if not args.identity: parser.error('导出需要 --identity')
        password = getpass.getpass('恢复口令（至少12位）：')
        if getpass.getpass('再次输入恢复口令：') != password:
            raise SystemExit('两次口令不一致，未导出')
        print(export_identity(args.identity, args.export, password))
    else:
        if not args.destination: parser.error('恢复需要 --destination')
        print(restore_identity(args.restore, args.destination, getpass.getpass('恢复口令：')))
