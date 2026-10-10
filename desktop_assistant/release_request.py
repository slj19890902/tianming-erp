"""Prepare and sign release manifests without moving the factory private key."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from desktop_assistant.storage import safe_name, sha


def public_identity(public_bytes):
    key = serialization.load_pem_public_key(public_bytes)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError('发布公钥必须为Ed25519')
    canonical = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, hashlib.sha256(canonical).hexdigest()


def inspect_package(package, public_bytes=None):
    """Stream every member; neither ZIP attributes nor a request sidecar is trust."""
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        names = [safe_name(entry.filename) for entry in entries]
        if len(set(name.casefold() for name in names)) != len(names):
            raise ValueError('发布包含重复路径')
        if any(stat.S_ISLNK(entry.external_attr >> 16) for entry in entries):
            raise ValueError('发布包不能含链接')
        if sum(entry.file_size for entry in entries) > 100 * 1024**3:
            raise ValueError('发布包超过大小限制')
        if archive.getinfo('manifest.json').file_size > 16 * 1024**2:
            raise ValueError('发布清单超过大小限制')
        raw = archive.read('manifest.json')
        manifest = json.loads(raw)
        if manifest.get('type') != 'tianming.release.v1':
            raise ValueError('不是程序发布包')
        files = manifest['files']
        expected = set(files) | {'manifest.json'}
        if public_bytes is not None:
            key, _ = public_identity(public_bytes)
            key.verify(archive.read('manifest.sig'), raw)
            expected.add('manifest.sig')
        if set(names) != expected:
            raise ValueError('发布包文件清单不一致')
        for name, expected_hash in files.items():
            safe_name(name)
            with archive.open(name) as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected_hash:
                raise ValueError('发布文件哈希不一致：' + name)
    return raw, manifest


def prepare_request(package, public_bytes, destination):
    raw, manifest = inspect_package(package)
    _, fingerprint = public_identity(public_bytes)
    record = {'type': 'tianming.release-signing-request.v1',
              'package_sha256': sha(package), 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
              'publisher_fingerprint': fingerprint, 'git_sha': manifest['git_sha'],
              'version': manifest['version'], 'revision': manifest['revision'],
              'runtime_platform': manifest.get('runtime_platform', 'windows'),
              'status': 'unsigned_not_installable'}
    with Path(destination).open('x', encoding='utf-8') as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
    return record


def sign_request(package, request_path, identity, signature_path, *, expected_git_sha, expected_manifest_sha256):
    from desktop_assistant.signing import load_key, public_bytes
    record = json.loads(Path(request_path).read_bytes())
    if record.get('type') != 'tianming.release-signing-request.v1':
        raise ValueError('签名请求格式错误')
    raw, manifest = inspect_package(package)
    if (record.get('package_sha256') != sha(package)
            or record.get('manifest_sha256') != hashlib.sha256(raw).hexdigest()
            or record.get('manifest_sha256') != expected_manifest_sha256
            or manifest.get('git_sha') != expected_git_sha
            or record.get('git_sha') != expected_git_sha):
        raise ValueError('签名请求与已审阅源码/清单不一致')
    for field, fallback in (('version', None), ('revision', None), ('runtime_platform', 'windows')):
        if record.get(field) != manifest.get(field, fallback):
            raise ValueError('签名请求元数据与程序包不一致')
    key = load_key(identity)  # Original factory Windows user unwraps DPAPI here.
    _, fingerprint = public_identity(public_bytes(key))
    if fingerprint != record.get('publisher_fingerprint'):
        raise ValueError('不是既有发布身份，拒绝更换公钥')
    signature = key.sign(raw)
    key.public_key().verify(signature, raw)
    with Path(signature_path).open('xb') as output:
        output.write(signature)
    return {'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'publisher_fingerprint': fingerprint}


def finalize_request(package, signature_path, public_bytes, destination):
    raw, _ = inspect_package(package)
    signature = Path(signature_path).read_bytes()
    key, _ = public_identity(public_bytes)
    key.verify(signature, raw)  # Refuse bad signature before producing output.
    destination = Path(destination)
    if destination.exists():
        raise ValueError('签名发布输出已存在，拒绝覆盖')
    pending = destination.with_name(destination.name + '.pending')
    with Path(package).open('rb') as source, pending.open('xb') as output:
        shutil.copyfileobj(source, output, 1024 * 1024)
    with zipfile.ZipFile(pending, 'a') as archive:
        archive.writestr('manifest.sig', signature)
    inspect_package(pending, public_bytes)
    # A destination created concurrently must not be replaced.
    with pending.open('rb') as source, destination.open('xb') as output:
        shutil.copyfileobj(source, output, 1024 * 1024)
    if sha(destination) != sha(pending):
        raise ValueError('发布包最终副本校验失败，禁止使用')
    return {'release_sha256': sha(destination), 'signed': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('sign', 'finalize'))
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--identity', type=Path)
    parser.add_argument('--signature', type=Path, required=True)
    parser.add_argument('--trusted-public-key', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--expected-git-sha')
    parser.add_argument('--expected-manifest-sha256')
    args = parser.parse_args()
    if args.action == 'sign':
        if not all((args.request, args.identity, args.expected_git_sha, args.expected_manifest_sha256)):
            parser.error('签名需要请求、原发布身份、已审阅git SHA和manifest SHA256')
        result = sign_request(args.package, args.request, args.identity, args.signature,
                              expected_git_sha=args.expected_git_sha,
                              expected_manifest_sha256=args.expected_manifest_sha256)
    else:
        if not args.trusted_public_key or not args.output:
            parser.error('合包需要既有可信公钥和新的输出路径')
        result = finalize_request(args.package, args.signature, args.trusted_public_key.read_bytes(), args.output)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
