"""Finite v717 exporter admission, stage A only. Never run from a user tree.

The offline preparer pins these bytes in a normal macOS authorization launcher.
Only a new root-protected transaction may run this entry. Failure retains it.
There is intentionally no capture, service operation, repair or retry command.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid
import zipfile

STAGE = Path('/Library/TianmingERP-UAT-Export-v717-c32fcafbb036')
TRANSACTION = STAGE / 'transaction'
ADMIN = STAGE / 'admin'
BASE = Path('/Library/TianmingERP')
OLD_CODE = Path('/Library/TianmingERP-Plan-v630e-20261014/base-code')
BOOT_SHA = 'a3fb7a143d73e2129f9237a07d910254584413e13139a84605f2cb0be3eac3cd'
BOOT_MANIFEST = 'ce817e4a16ecd021661fa8e6657f37f0f265f7b4ae7e5085d25c881a6090a473'
LIVE_PACKAGE = 'ea1f60c7be10e237d796fdfc79d7ad1372ff8e23ef42be24bc5f515b3b2ba1e6'
LIVE_SOURCE = '3a8d76000ea01685ea6ba0ca465c007e1826dd73'
LIVE_MANIFEST = '0fb139f39eea21073c11513172ecc41c26ac959c215c2dbbd07bbc5ea21c22e2'
LIVE_TREE = BASE / 'published' / LIVE_PACKAGE / 'migration'
RUNTIME = LIVE_TREE / 'runtime/bin/python3.12'
TOOL_SOURCE = 'c32fcafbb03602ca5448cdc4f9fc1345fc63d721'
TOOL_MANIFEST = '613a428b90a116b5f83ad370e9ae662ca7513afa1265b0947fdc0281d7119284'
UNSIGNED_SHA = '051e3bc4c03a651230deac414a810bb93ba31771c70a23ac3720fb0c1121ab65'
ADMIN_SHA = 'e849242a80cdc19774c105999be80f2c64d7e072f5c6002cdc8003927d4e7386'
ADMIN_MANIFEST = '3767d25af76162cc797fb7a6e79d33600f89bfdb0e9b8fb55749add841d469ef'
SIGNER = Path('/Library/TianmingERP-MacPublisher-v4-20261013/run-identity.py')
SIGNER_SHA = '357c440ccda41ea91dea60a6d8583b719e218799222cbe90546c55c2e187fc1c'
SUPERVISOR = 'a1df45a68a604a3942219a34c62e8a5058d04d9943ee3fb94c8ed7db42135729'
INPUT = Path('/Users/kimiyf/ERP-Migration/evidence/erp3-next-20261023/controlled-export-v717/environment-compat')
SOURCE = Path('/Users/kimiyf/ERP-Migration/controlled-uat-export-v717-20261023/scripts/admin/controlled_uat_env_compat_admission_v717.py')
INPUTS = {
    'admin.zip': (INPUT / 'admin-v717-c32fcafbb036.zip', ADMIN_SHA, 107923),
    'manifest.json': (INPUT / 'unsigned-v717-c32fcafbb036/manifest.json', TOOL_MANIFEST, None),
    'release.unsigned.zip': (INPUT / 'unsigned-v717-c32fcafbb036/release.unsigned.zip', UNSIGNED_SHA, 536433931),
}
ENV = {'HOME': str(BASE / 'home'), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'}
LIMIT = 16 * 1024**2


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'Duplicate JSON field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            h.update(block)
    return h.hexdigest()


def protected_file(path, boot, *, mode=0o444, limit=LIMIT, expected=None):
    for parent in path.parents:
        boot.protected(parent, directory=True, readable=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == before.st_gid == 0
                and stat.S_IMODE(before.st_mode) == mode and before.st_nlink == 1
                and 0 < before.st_size <= limit, 'Invalid protected input')
        boot.no_acl(stream.fileno())
        raw = stream.read(limit + 1)
        require(stamp(before) == stamp(os.fstat(stream.fileno()))
                == stamp(path.lstat()) and len(raw) <= limit, 'Input changed')
        require(expected is None or digest(raw) == expected, 'Input digest mismatch')
        return raw


def trusted_boot(*, apple=True):
    # No project import until the existing immutable bootstrap is authenticated.
    p = OLD_CODE / 'desktop_assistant/mac_keychain_install.py'
    for item in (p, *p.parents):
        info = item.lstat()
        require(info.st_uid == 0 and not info.st_mode & 0o022
                and not stat.S_ISLNK(info.st_mode), 'Unprotected bootstrap path')
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'Invalid bootstrap')
        raw = stream.read(65537)
        require(digest(raw) == BOOT_SHA and stamp(before) == stamp(os.fstat(stream.fileno()))
                == stamp(p.lstat()), 'Bootstrap digest changed')
    spec = importlib.util.spec_from_loader('fixed_admission_boot', loader=None)
    boot = importlib.util.module_from_spec(spec)
    boot.__file__ = str(p)
    exec(compile(raw, str(p), 'exec'), boot.__dict__)
    boot.protected(p, readable=True)
    require(boot.verify_code() == BOOT_MANIFEST, 'Existing bootstrap pack changed')
    if apple:
        boot.verify_python(current=True)
    return boot


def admin_contents(raw, expected_files):
    """Pure bounded archive validation; never extract caller-supplied paths."""
    require(digest(raw) == ADMIN_SHA, 'Admin ZIP digest mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        expected = set(expected_files) | {'install-manifest.json'}
        require(len(entries) == len(expected) and {e.filename for e in entries} == expected,
                'Admin ZIP inventory mismatch')
        for entry in entries:
            require(entry.create_system == 3 and entry.external_attr >> 16 == 0o100444
                    and entry.compress_type == zipfile.ZIP_DEFLATED and not entry.flag_bits & 1
                    and 0 < entry.file_size <= 65536, 'Invalid admin ZIP member')
        contents = {name: archive.read(name) for name in expected}
    manifest = decode(contents['install-manifest.json'])
    require(digest(contents['install-manifest.json']) == ADMIN_MANIFEST
            and set(manifest) == {'type', 'source', 'files'}
            and manifest['type'] == 'tianming.keychain-code.v1'
            and manifest['source'] == TOOL_SOURCE
            and set(manifest['files']) == set(expected_files), 'Admin manifest mismatch')
    require(all(digest(contents[name]) == manifest['files'][name] for name in expected_files),
            'Admin member digest mismatch')
    return contents


def new_directory(path, boot):
    path.mkdir(mode=0o755)  # Existing/partial directories fail; no repair.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o755)
        info = os.fstat(fd)
        require(info.st_uid == info.st_gid == 0, 'Wrong new directory owner')
        boot.no_acl(fd)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path, raw, boot, *, mode=0o444):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    require(protected_file(path, boot, mode=mode, limit=max(len(raw), 1)) == raw,
            'New file readback mismatch')


def install_admin(contents, boot):
    new_directory(ADMIN, boot)
    directories = sorted({p for name in contents for p in Path(name).parents if str(p) != '.'},
                         key=lambda p: (len(p.parts), str(p)))
    for relative in directories:
        new_directory(ADMIN / relative, boot)
    for name, raw in sorted(contents.items()):
        write_new(ADMIN / name, raw, boot)


def copy_bound(source, destination, expected, size, boot):
    """Copy only fixed public program bytes; never execute user-writable input."""
    for item in (source, *source.parents):
        require(not stat.S_ISLNK(item.lstat().st_mode), 'Source path contains a link')
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as src:
        before = os.fstat(src.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 501 and before.st_nlink == 1
                and not before.st_mode & 0o022 and 0 < before.st_size <= 1024**3
                and (size is None or before.st_size == size), 'Invalid fixed input')
        out = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        h = hashlib.sha256()
        with os.fdopen(out, 'wb') as dst:
            os.fchmod(dst.fileno(), 0o444)
            for chunk in iter(lambda: src.read(1024**2), b''):
                h.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
            boot.no_acl(dst.fileno())
        require(h.hexdigest() == expected and stamp(before) == stamp(os.fstat(src.fileno()))
                == stamp(source.lstat()), 'Input changed while copying; retain failed stage')
    require(file_hash(destination) == expected, 'Protected copy digest mismatch')


def ingress(raw, boot):
    require(Path(__file__).absolute() == SOURCE and not sys.argv[1:]
            and isinstance(raw, bytes) and 0 < len(raw) <= 65536, 'Invalid launcher admission')
    for parent in STAGE.parents:
        boot.protected(parent, directory=True, readable=True)
    new_directory(STAGE, boot)
    new_directory(TRANSACTION, boot)
    write_new(TRANSACTION / 'stage-a.py', raw, boot)
    for name, (source, expected, size) in INPUTS.items():
        copy_bound(source, TRANSACTION / name, expected, size, boot)
    # Only the same SHA-admitted bytes now stored under root are executable.
    with (TRANSACTION / 'stage-a.stdout').open('xb') as out, (TRANSACTION / 'stage-a.stderr').open('xb') as err:
        os.fchmod(out.fileno(), 0o644)
        os.fchmod(err.fileno(), 0o600)
        child = subprocess.Popen(['/usr/bin/python3', '-I', '-S', '-B', str(TRANSACTION / 'stage-a.py')],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, cwd='/', env=ENV,
            close_fds=True, start_new_session=True, umask=0o077)
    print(json.dumps({'stage': str(STAGE), 'pid': child.pid, 'captured': False,
                      'scope': 'fixed exporter admission and inspect plan only'}), flush=True)


def approved_source(source):
    require(source['application_package_sha256'] == LIVE_PACKAGE
            and source['application_git_sha'] == LIVE_SOURCE and source['revision'] == 'fa1023oc',
            'Live application changed; new candidate review required')


def current_application(boot):
    path = BASE / 'instance/state.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_uid == before.st_gid == 499
                and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1,
                'Unexpected service state identity')
        boot.no_acl(stream.fileno())
        raw = stream.read(LIMIT + 1)
        require(len(raw) <= LIMIT and stamp(before) == stamp(os.fstat(stream.fileno()))
                == stamp(path.lstat()), 'Service state changed during read')
    value = decode(raw)
    require(value['current'] == LIVE_PACKAGE and value['operation'] == 'update'
            and value.get('manual_stop') is False, 'Current application is no longer the approved v717')


def run_fixed(argv, *, raw=None):
    # Every caller below supplies a fixed executable/action; no shell or caller argv.
    result = subprocess.run(argv, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd='/', env=ENV, close_fds=True, timeout=1800, umask=0o077)
    require(result.returncode == 0, 'Fixed child failed; retain transaction, do not retry')
    return result.stdout


def finalize(boot):
    require(Path(sys.executable).resolve() == RUNTIME.resolve(), 'Wrong finalization interpreter')
    manifest_raw = protected_file(LIVE_TREE / 'manifest.json', boot, mode=0o644, expected=LIVE_MANIFEST)
    manifest = decode(manifest_raw)
    require(file_hash(RUNTIME) == manifest['files']['runtime/bin/python3.12'], 'Runtime changed')
    require(file_hash(TRANSACTION / 'release.unsigned.zip') == UNSIGNED_SHA, 'Unsigned ZIP changed')
    sys.path.insert(0, str(LIVE_TREE))
    from desktop_assistant.release_request import finalize_request
    result = finalize_request(TRANSACTION / 'release.unsigned.zip', TRANSACTION / 'manifest.sig',
                              boot.protected(BASE / 'mac-publisher-public.pem', readable=True),
                              TRANSACTION / 'release.signed.zip')
    os.chmod(TRANSACTION / 'release.signed.zip', 0o644)  # Only this newly created output.
    write_new(TRANSACTION / 'signed-result.json', boot.canonical(result), boot)


def stage_a(boot):
    current_application(boot)
    extra = {'desktop_assistant/uat_export_contract.py', 'desktop_assistant/uat_export_sanitize.py',
             'desktop_assistant/mac_uat_export.py'}
    names = set(boot.FILES) | extra
    require(len(names) == 25, 'Unexpected existing admin contract')
    contents = admin_contents(protected_file(TRANSACTION / 'admin.zip', boot, limit=2*1024**2), names)
    raw = protected_file(TRANSACTION / 'manifest.json', boot, expected=TOOL_MANIFEST)
    manifest = decode(raw)
    require((manifest['git_sha'], manifest['version'], manifest['revision'], manifest['migration'])
            == (TOOL_SOURCE, 'v0.22.717', 'fa1023oc', None), 'Tool identity mismatch')
    require(file_hash(TRANSACTION / 'release.unsigned.zip') == UNSIGNED_SHA, 'Unsigned ZIP changed')
    install_admin(contents, boot)
    sys.path.insert(0, str(ADMIN))
    from desktop_assistant import mac_keychain_install as new_boot
    from desktop_assistant import mac_release_publish as pub, mac_publisher_trust as trust
    from desktop_assistant import mac_uat_export as export
    require(new_boot.verify_code() == ADMIN_MANIFEST, 'Installed admin pack failed verification')
    # Inspect with the old application package cannot run the new inspect action.
    # The existing system metadata and control verify it without querying business rows.
    from desktop_assistant import mac_system_service as system
    live_meta = system.metadata(LIVE_PACKAGE)
    supervisor_meta = system.metadata(SUPERVISOR)
    control_before, _ = system.checked_control(supervisor_meta)
    plist_before = system._installed_plist(supervisor_meta)
    live_raw = protected_file(LIVE_TREE / 'manifest.json', boot, mode=0o644, expected=LIVE_MANIFEST)
    pub.check_tree(LIVE_TREE, decode(live_raw), live_raw, server=False)
    require(not SIGNER.lstat().st_mode & 0o022, 'Signer is writable')
    protected_file(SIGNER, boot, mode=stat.S_IMODE(SIGNER.lstat().st_mode), expected=SIGNER_SHA)
    signed = decode(run_fixed(['/usr/bin/python3', '-I', '-S', '-B', str(SIGNER), 'sign', TOOL_MANIFEST], raw=raw))
    signature = bytes.fromhex(signed['signature_hex'])
    require(len(signature) == 64, 'Invalid signature length')
    write_new(TRANSACTION / 'manifest.sig', signature, boot)
    run_fixed([str(RUNTIME), '-I', '-B', str(TRANSACTION / 'stage-a.py'), '_finalize'])
    result = decode(protected_file(TRANSACTION / 'signed-result.json', boot))
    target = file_hash(TRANSACTION / 'release.signed.zip')
    require(result == {'release_sha256': target, 'signed': True}, 'Finalized package mismatch')
    with (TRANSACTION / 'release.signed.zip').open('rb') as stream:
        actual_raw, actual_manifest, _ = pub.inspect(stream)
    require(actual_raw == raw and actual_manifest == manifest, 'Finalized manifest changed')
    context = new_boot.protected(BASE / 'keychain-context.json', readable=True)
    require(new_boot.digest(context) == live_meta['context_sha256'], 'Service identity changed')
    with pub.lock():
        authority = trust.read_authority(expected_context_sha256=new_boot.digest(context))
        updated = trust.authorize_package(authority['record'], target, TOOL_MANIFEST)
        write_new(TRANSACTION / 'prior-trust.json', authority['raw'], boot)
        temporary = BASE / ('mac-publisher-trust-' + uuid.uuid4().hex + '.approved.json')
        new_boot._write(temporary, trust.canonical(updated), mode=0o644)
        require(trust.protected_read(trust.RECORD) == authority['raw'], 'Trust changed')
        os.replace(temporary, trust.RECORD)
        pub.sync_directory(BASE)
        require(trust.read_authority()['record'] == updated, 'Trust readback failed')
    now = int(time.time())
    legacy = authority['legacy_public']
    plan = dict(type=pub.TYPE, attempt_id=uuid.uuid4().hex, created_at=now, expires_at=now+3600,
                code_manifest_sha256=ADMIN_MANIFEST, context_sha256=new_boot.digest(context),
                package_sha256=target, package_bytes=(TRANSACTION/'release.signed.zip').stat().st_size,
                manifest_sha256=TOOL_MANIFEST, public_key=legacy.decode('ascii'),
                publisher_sha256=new_boot.digest(legacy))
    plan_raw = new_boot.canonical(plan)
    write_new(TRANSACTION / 'publication-plan.json', plan_raw, boot)
    publication = pub.publish(plan_raw, new_boot.digest(plan_raw), TRANSACTION / 'release.signed.zip')
    require(publication['package_sha256'] == target and publication['signature_verified_by_service_uid']
            and publication['factory_activated'] is False, 'Publication did not complete')
    write_new(TRANSACTION / 'publication-result.json', new_boot.canonical(publication), boot)
    inspected = export.make_plan(target)
    require(inspected['captured'] is False, 'Unexpected capture')
    current_application(boot)
    approved_source(inspected['plan']['source'])
    require(inspected['plan']['supervisor_package_sha256'] == SUPERVISOR
            and system.checked_control(supervisor_meta)[0] == control_before
            and system._installed_plist(supervisor_meta) == plist_before, 'Supervisor changed')
    write_new(TRANSACTION / 'export-plan-result.json', new_boot.canonical(inspected), boot)
    write_new(TRANSACTION / 'stage-a-result.json', new_boot.canonical({
        'status': 'stage_a_complete_plan_requires_separate_approval', 'package_sha256': target,
        'admin_manifest_sha256': ADMIN_MANIFEST, 'plan_sha256': inspected['plan_sha256'],
        'attempt_id': inspected['attempt_id'], 'captured': False, 'service_operation': False}), boot)


def main():
    require((os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (0, 0, 0, 0)
            and sys.flags.isolated and sys.dont_write_bytecode, 'Requires isolated root entry')
    admitted = globals().get('_LAUNCHER_ADMITTED')
    if admitted is not None:
        ingress(admitted, trusted_boot())
        return
    require(Path(__file__).absolute() == TRANSACTION / 'stage-a.py', 'Only protected admission may execute')
    require(sys.argv[1:] in ([], ['_finalize']), 'No configurable actions or paths')
    boot = trusted_boot(apple=not sys.argv[1:])
    protected_file(TRANSACTION / 'stage-a.py', boot)
    if sys.argv[1:]:
        finalize(boot)
    else:
        stage_a(boot)


if __name__ == '__main__':
    main()
