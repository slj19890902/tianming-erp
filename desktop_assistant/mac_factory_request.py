"""Dedicated, read-only target request check; never activate, start or decrypt."""
import os
from pathlib import Path
import sys

from desktop_assistant import mac_keychain_install as boot, mac_system_service as system
from desktop_assistant import mac_published_runtime as published, mac_service_manage as manage
from desktop_assistant import factory_cutover as cutover, factory_network as network
from desktop_assistant import mac_factory_request_install as installation
from app.core import mac_service_keychain as keychain


def run(value, *, script, executable):
    if (set(value) != {'request', 'network', 'challenge', 'context_sha256', 'registry_sha256'}
            or not sys.flags.isolated or not sys.dont_write_bytecode or not keychain.selected()):
        raise ValueError('目标申请不是隔离专用身份请求')
    cutover._id(value['challenge'])
    for k in ('context_sha256', 'registry_sha256'): cutover._hash(value[k])
    request = cutover.validate_request(value['request']); proposed = network.validate(value['network'])
    if request['network_sha256'] != cutover.digest(proposed): raise ValueError('网络配置与申请摘要不符')
    context = keychain.load_context()
    if boot._actual() != boot._expected(context['uid'], context['gid']): raise ValueError('实际专用身份不符')
    meta = system.metadata(request['package_sha256'])
    if (context != meta['context'] or any(value[k] != meta[k] for k in ('context_sha256','registry_sha256'))
            or request['publisher_sha256'] != meta['publisher_sha256']):
        raise ValueError('本机批准身份变化')
    tree, manifest = published.resolve(manage.ROOT, request['package_sha256'], meta['public'], kind='migration')
    installation.require_protocol(manifest)
    if (Path(script).resolve() != tree/installation.ENTRY or Path(__file__).resolve() != tree/installation.MODULE
            or Path(executable).resolve() != tree/'runtime/bin/python3.12'):
        raise ValueError('目标申请未使用批准的不可变入口')
    with manage.instance(context) as (folders, lock, recheck), manage.maintenance(lock):
        if cutover.local_target(manage.ROOT, context['installation_id']) != request['target']:
            raise ValueError('目标申请与实际本机/账号/目录不符')
        _, state = manage.record(folders['root'], 'state.json', context)
        if (state.get('current') != request['package_sha256'] or state.get('manual_stop') is not True
                or state.get('onboarding_pending') or state.get('operation') == 'migration_running'
                or 'failed' in state.get('operation', '')):
            raise ValueError('只能为完整且明确停止的当前原生程序申请切换')
        from desktop_assistant.manager import Manager
        manager = Manager(manage.ROOT, meta['public'])
        if manager._process(): raise ValueError('目标ERP仍运行，不自动停止')
        manager._approve_execution(state['current'])
        recheck()
    return installation.expected_reply(value, context)


def main(*, script, executable):
    value = boot.decode(sys.stdin.buffer.read(boot.LIMIT+1))
    result = run(value, script=script, executable=executable)
    sys.stdout.buffer.write(boot.canonical(result)+b'\n'); sys.stdout.buffer.flush()
