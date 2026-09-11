"""Explicit publisher initialization; builds never rotate signing identity."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
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
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('发布身份目录非空，拒绝替换或创建另一身份')
    directory.mkdir(parents=True, exist_ok=True)
    sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True,
                                  creationflags=subprocess.CREATE_NO_WINDOW).strip().split(',')[-1].strip('"')
    subprocess.run(['icacls', str(directory), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F',
                    '*S-1-5-18:(OI)(CI)F'], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    key = Ed25519PrivateKey.generate()
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--initialize', type=Path, required=True)
    print(initialize(parser.parse_args().initialize))
