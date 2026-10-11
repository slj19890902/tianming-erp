"""Read-only execution resolver for administrator-published service code.

This creates no publication/activation record and never authorizes factory mode.
Writable instance caches remain backup/restore material, not executable code for
the dedicated UID. A future root installer must publish both immutable trees.
"""
import hashlib
import json
import os
from pathlib import Path
import re

from app.core import mac_service_keychain as keychain
from desktop_assistant.release_request import public_identity
from desktop_assistant.release_verification import immutable, verify_tree
from desktop_assistant.runtime_platform import runtime_python
from desktop_assistant.storage import read_json, sha, signed_release_manifest

BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
PUBLISHED = BASE / 'published'
REGISTRY = BASE / 'published.json'
PUBLIC = BASE / 'publisher-public.pem'
TYPE = 'tianming.mac-published.v1'


def selected():
    return keychain.selected()


def _read(path, limit=65536):
    with keychain._checked_path(path, uid=0, mode=0o644) as fd:
        raw = os.read(fd, limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError('受保护发布记录长度无效')
    return raw


def _record(raw):
    def unique(pairs):
        result = {}
        for k, value in pairs:
            if k in result: raise ValueError('发布记录重复字段')
            result[k] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {'type', 'context_sha256', 'publisher_sha256', 'packages'}
            or value['type'] != TYPE or any(not isinstance(value[k], str)
                or not re.fullmatch('[0-9a-f]{64}', value[k]) for k in ('context_sha256', 'publisher_sha256'))
            or not isinstance(value['packages'], list) or not 1 <= len(value['packages']) <= 32
            or any(not isinstance(item, str) or not re.fullmatch('[0-9a-f]{64}', item) for item in value['packages'])
            or value['packages'] != sorted(set(value['packages']))):
        raise ValueError('受保护发布记录结构不完整')
    return value


def resolve(root, identity, public, *, kind='server'):
    return _verified_tree(root, identity, public, kind=kind, executing=True)


def verify_authority(root, identity, public):
    """Authenticate offline schema evidence; never return an executable path.

    Preserve approval, signature, immutable protocol and complete tree checks.
    Only the runtime system capability is inapplicable to offline evidence.
    """
    for kind in ('server', 'migration'):
        _, manifest = _verified_tree(root, identity, public, kind=kind, executing=False)
    if read_json(Path(root)/'releases'/identity/'manifest.json') != manifest:
        raise ValueError('结构权威缓存与root签名原件不一致')
    return manifest


def _verified_tree(root, identity, public, *, kind, executing):
    if kind not in {'server', 'migration'} or not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{64}', identity):
        raise ValueError('执行副本身份或用途无效')
    if Path(root).absolute() != ROOT or Path(root).resolve() != ROOT:
        raise ValueError('专用服务只允许固定本机实例')
    context = keychain.load_context()  # Root metadata/real service identity only; no secret reads.
    with keychain._checked_path(ROOT, uid=context['uid'], directory=True, mode=0o700):
        pass
    record = _record(_read(REGISTRY))
    # Bind exact root context bytes; do not silently substitute an equivalent record.
    context_raw = _read(keychain.CONTEXT)
    pinned_public = _read(PUBLIC)
    if (hashlib.sha256(context_raw).hexdigest() != record['context_sha256']
            or public_identity(pinned_public)[1] != record['publisher_sha256']
            or public_identity(public)[1] != record['publisher_sha256']
            or identity not in record['packages']):
        raise ValueError('程序尚未获本机管理员发布批准，未停止服务')
    slot = PUBLISHED / identity
    for directory in (PUBLISHED, slot):
        immutable(directory, directory=True)
    if {path.name for path in slot.iterdir()} != {'package.zip', 'server', 'migration'}:
        raise ValueError('root发布目录不完整或含额外对象')
    package = slot / 'package.zip'
    immutable(package)
    if sha(package) != identity:
        raise ValueError('root发布原包摘要不符')
    manifest = signed_release_manifest(package, pinned_public)
    if manifest.get('runtime_platform') != 'macos-arm64':
        raise ValueError('专用Mac服务不能执行Windows运行器')
    capabilities = manifest.get('runtime_capabilities')
    capability = capabilities.get('immutable_execution_v1') if isinstance(capabilities, dict) else None
    if type(capability) is not int or capability != 1:
        raise ValueError('已签名程序尚不支持不可变系统执行入口')
    service_control = BASE / 'system-service.json'
    if executing and (service_control.exists() or service_control.is_symlink()):
        service_capability = capabilities.get('system_service_v1')
        if type(service_capability) is not int or service_capability != 1:
            raise ValueError('已安装系统监护，程序必须保留管理员停用门禁')
    if executing and os.path.lexists(BASE / 'factory-activation.json'):
        from desktop_assistant.mac_factory_runtime import require_execution
        require_execution(manifest)
    required = {'main.py', 'app/main.py', 'desktop_assistant/server_entry.py'}
    required.update({'runtime/ocr/model/craft_mlt_25k.pth', 'runtime/ocr/model/zh_sim_g2.pth',
                     'runtime/ocr/user_network/.keep'})
    if kind == 'migration':
        required.add('desktop_assistant/migration_entry.py')
    if not required <= set(manifest.get('files', {})):
        raise ValueError('已签名程序缺少对应执行入口，未停止服务')
    release = verify_tree(ROOT, slot / kind, manifest, root_owned=True, managed_links=kind == 'server')
    runtime_python(release, manifest, runnable=True)
    return release, manifest


def environment(value):
    # A shared environment file is business configuration, not interpreter or
    # loader configuration. -I ignores Python overrides and the user site too.
    result = {k: v for k, v in value.items()
              if k.startswith('ERP_') or k in {'OPENAI_API_KEY', 'DEEPSEEK_API_KEY'}}
    result.update(HOME=str(BASE / 'home'), PATH='/usr/bin:/bin:/usr/sbin:/sbin', LANG='en_US.UTF-8')
    return result
