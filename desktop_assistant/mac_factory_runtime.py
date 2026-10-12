"""Consume protected activation at startup; never create or repair authority.

Configuration is a root record plus a source signature and restored provenance,
not an environment flag. The admission latch is local to the verified server
process. This is startup authorization, not a sandbox against arbitrary code
already running as the writable database owner.
"""
from contextlib import ExitStack
import os
from pathlib import Path
import sys

from app.core import mac_service_keychain as keychain
from desktop_assistant import mac_published_runtime as published
from desktop_assistant import factory_activation_contract as contract, factory_cutover as protocol
from desktop_assistant import mac_keychain_install as boot
from desktop_assistant.mac_service_restore import Lease, _read_complete
from desktop_assistant.release_request import public_identity

MODULE = 'desktop_assistant/mac_factory_runtime.py'
ENTRY = 'desktop_assistant/server_entry.py'
EXECUTION_FILES = {MODULE, ENTRY, 'app/main.py', 'app/core/home_rehearsal.py',
                   'desktop_assistant/manager.py', 'desktop_assistant/service_supervisor.py'}
_admitted = None


def require_execution(manifest):
    contract.require_protocol(manifest)
    if not EXECUTION_FILES <= manifest.get('files', {}).keys():
        raise ValueError('正式运行入口不完整')


def configuration(root, package, public):
    """Authenticated configured mode; no secret/DB reads, expiry or writes.

    Existing invalid authority raises. Only an absent root record means home.
    The current approved package can advance; original restore binding cannot.
    Executable tree/capability checks remain mandatory at each launch.
    """
    if not published.selected(): return None
    root = Path(root)
    if root.absolute() != published.ROOT or root.resolve() != published.ROOT:
        raise ValueError('正式模式只允许固定专用实例')
    active = published.BASE/contract.NAME
    if not os.path.lexists(active): return None
    from desktop_assistant import mac_service_manage as manage
    context = keychain.load_context()
    with ExitStack() as stack:
        base = stack.enter_context(Lease(published.BASE,uid=0,gid=None,mode=0o755,directory=True,mutable=True))
        held = []
        def read(path, parent=base, *, uid=0, gid=None, mode=0o644):
            item=stack.enter_context(Lease(path,uid=uid,gid=gid,mode=mode,parent=parent));held.append(item)
            limit=manage.STATE_LIMIT if path==root/'state.json' else contract.LIMIT
            return _read_complete(item,limit)
        raw=read(active);value=contract.decode(raw);body=contract.validate_record(value)
        context_raw=read(keychain.CONTEXT);registry=published._record(read(published.REGISTRY));pinned=read(published.PUBLIC)
        request=value['request'];context_sha=boot.digest(context_raw)
        # The saved registry SHA describes the activation transaction. Approved
        # upgrades extend the current registry; preserve its context/publisher
        # and both the original and current package approvals instead of
        # requiring the historical entire registry to stay byte-identical.
        if (boot.decode(context_raw)!=context or value['context_sha256']!=context_sha
                or registry['context_sha256']!=context_sha
                or public_identity(pinned)[1]!=request['publisher_sha256']
                or public_identity(public)[1]!=request['publisher_sha256']
                or registry['publisher_sha256']!=request['publisher_sha256']
                or package not in registry['packages'] or request['package_sha256'] not in registry['packages']
                or request['target']!=protocol.local_target(root,context['installation_id'])):
            raise ValueError('正式激活与本机、身份、程序批准或发布公钥不符')
        protocol.verify_authorization(value['authorization'].encode(),pinned,request)
        # activated_at was inside the signed installation window. Do not apply
        # that old deadline to a server which has since been used or updated.
        if contract._time(value['activated_at']) > contract._now():
            raise ValueError('正式激活时间晚于本机当前时间')
        original=contract.decode(read(published.BASE/('factory-target-'+request['request_id']+'.json')))
        network=contract.decode(read(published.BASE/('factory-network-'+request['request_id']+'.json')))
        if boot.canonical(original)!=boot.canonical(request) or boot.canonical(network)!=boot.canonical(value['network']):
            raise ValueError('正式激活的原申请或网络记录已改变')
        private=stack.enter_context(Lease(root,uid=context['uid'],gid=context['gid'],mode=0o700,
                                          directory=True,parent=base,mutable=True))
        state=manage.decode_record(read(root/'state.json',private,uid=context['uid'],gid=context['gid'],mode=0o600),'state.json')
        binding={'type':'tianming.mac-native-restore.v1','package_sha256':request['package_sha256'],
            'backup_sha256':body['source']['backup_sha256'],'context_sha256':context_sha,
            'database_revision':body['source']['snapshot']['revision'],'schema_migrated':False,'factory_cutover':False}
        if (state.get('current')!=package or state.get('restored_backup_sha256')!=body['source']['backup_sha256']
                or boot.canonical(state.get('native_restore_binding'))!=boot.canonical(binding)):
            raise ValueError('正式实例不是已核验的最终恢复来源')
        for item in held:item.check()
        return {'network':dict(value['network']),'activation_sha256':boot.digest(raw),
                'package_sha256':package,'request_id':request['request_id']}


def configured_mode(manager):
    """Public status only. Invalid authority must not prevent status or stop."""
    if not published.selected(): return 'home'
    try:
        value=configuration(manager.root,manager.state.get('current'),manager.public_key)
        return 'factory' if value else 'home'
    except Exception:
        return 'attention'


def require_factory_service():
    """Startup admission, never used by status/stop or as physical proof."""
    from desktop_assistant import mac_system_service as system
    context=keychain.load_context()
    system.check_start()
    if boot.canonical(system.management_service_state()) != boot.canonical(
            {'registered':True,'enabled':True,'scheduled_backups':True}):
        raise ValueError('正式服务需要已注册启用的系统监护与备份协议')
    system.verify_keychain(context)



def _identity():
    if (sys.platform!='darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or not keychain.selected()):
        raise ValueError('受管服务必须使用隔离的实际专用身份')
    return keychain.load_context()


def _fixed_environment(environment):
    root=published.ROOT
    expected={'ERP_DATABASE_PATH':str(root/'shared/data/carton_erp.sqlite3'),
              'TM_ERP_CONTROL':str(root/'control'),'ERP_WORKERS':'1'}
    if any(environment.get(k)!=v for k,v in expected.items()) or environment.get('ERP_UAT_ROOT'):
        raise ValueError('服务进程的数据库、控制目录或工作进程配置不符')
    protocol._id(environment.get('TM_ERP_NONCE'))


def admit_server(*, script, executable):
    """Called before application imports or external access in the server only."""
    global _admitted
    if not published.selected(): return
    _admitted = None
    _identity();_fixed_environment(os.environ)
    public=published._read(published.PUBLIC)
    from desktop_assistant.storage import read_json
    state=read_json(published.ROOT/'state.json');package=state.get('current')
    tree,manifest=published.resolve(published.ROOT,package,public)
    if (Path(script).resolve()!=tree/ENTRY or Path(__file__).resolve()!=tree/MODULE
            or Path(executable).resolve()!=tree/'runtime/bin/python3.12'):
        raise ValueError('服务入口不是当前批准的不可变程序')
    value=configuration(published.ROOT,package,public)
    if value:
        require_execution(manifest)
        expected={**value['network'],'ERP_FACTORY_ACTIVATION_SHA256':value['activation_sha256'],
                  'ERP_FACTORY_PACKAGE_SHA256':package,'ERP_ENVIRONMENT':'production'}
    else:
        expected={'ERP_HOME_REHEARSAL':'1','ERP_BIND_HOST':'127.0.0.1','ERP_AI_INVENTORY_PROVIDER':'disabled'}
        if any(k in os.environ for k in ('OPENAI_API_KEY','DEEPSEEK_API_KEY','ERP_FACTORY_ACTIVATION_SHA256','ERP_FACTORY_PACKAGE_SHA256')):
            raise ValueError('家庭服务进程含外部凭据或正式模式标记')
    if any(os.environ.get(k)!=v for k,v in expected.items()):
        raise ValueError('服务进程环境与已核验运行模式不符')
    if value: require_factory_service()
    # Direct use of the approved entry must preserve the same schema/readers as
    # Manager.start. Avoid Manager.__init__, which creates missing directories.
    if (state.get('onboarding_pending') or state.get('operation') == 'migration_running'
            or 'failed' in state.get('operation', '')):
        raise ValueError('未决维护不能通过直接服务入口绕过')
    from desktop_assistant.manager import Manager
    from desktop_assistant.storage import database_info
    manager=object.__new__(Manager);manager.root=published.ROOT;manager.public_key=public
    manager._check_dispatch_reader(package,require_active=manager._dispatch_reader(package))
    manager._check_provenance_reader(package)
    if not manager.compatible(package,database_info(published.ROOT/'shared/data/carton_erp.sqlite3')['revision']):
        raise ValueError('服务入口程序与实际数据库或reader不兼容')
    _admitted={'pid':os.getpid(),'tree':tree,'expected':expected,
               'database':os.environ['ERP_DATABASE_PATH'],'control':os.environ['TM_ERP_CONTROL'],
               'factory':bool(value)}


def require_admitted_factory():
    if not published.selected(): return
    if (_admitted is None or _admitted['pid']!=os.getpid() or not _admitted['factory']
            or any(os.environ.get(k)!=v for k,v in _admitted['expected'].items())
            or os.environ.get('ERP_DATABASE_PATH')!=_admitted['database']
            or os.environ.get('TM_ERP_CONTROL')!=_admitted['control']
            or os.environ.get('ERP_WORKERS')!='1' or os.environ.get('ERP_UAT_ROOT')):
        raise ValueError('专用正式服务缺少本进程准入或运行环境已改变')


def require_application(module):
    if not published.selected(): return
    if (_admitted is None or _admitted['pid']!=os.getpid()
            or Path(module).resolve()!=_admitted['tree']/'app/main.py'):
        raise ValueError('专用应用必须通过已核验的服务入口启动')
    if _admitted['factory']:require_admitted_factory()
    elif (any(os.environ.get(k)!=v for k,v in _admitted['expected'].items())
            or os.environ.get('ERP_DATABASE_PATH')!=_admitted['database']
            or os.environ.get('TM_ERP_CONTROL')!=_admitted['control']
            or os.environ.get('ERP_WORKERS')!='1' or os.environ.get('ERP_UAT_ROOT')
            or any(k in os.environ for k in ('OPENAI_API_KEY','DEEPSEEK_API_KEY'))):
        raise ValueError('家庭服务不能在导入应用前改变已核验的运行环境')
