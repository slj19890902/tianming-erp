"""Short-lived native probe. Only newly owned synthetic keychains are written."""
import ctypes as C
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core.mac_service_keychain import Security, _keychain_file


def setup():
    api = Security()
    p = C.c_void_p
    for name, args in [
        ('SecKeychainDelete', [p]), ('SecKeychainLock', [p]),
        ('SecKeychainCopyDomainSearchList', [C.c_int, C.POINTER(p)]),
    ]:
        method = getattr(api.api.sec, name)
        method.argtypes, method.restype = args, C.c_int32
    api.api.cf.CFEqual.argtypes, api.api.cf.CFEqual.restype = [p, p], C.c_bool
    return api


def snapshot(api):
    refs = []
    for domain in (0, 1):
        for function in (api.api.sec.SecKeychainCopyDomainDefault,
                         api.api.sec.SecKeychainCopyDomainSearchList):
            ref = C.c_void_p()
            api.api.check(function(domain, C.byref(ref)))
            refs.append(ref.value)
    return refs


def equal(api, first, second):
    return all(api.api.cf.CFEqual(a, b) for a, b in zip(first, second))


def fingerprint():
    api = setup(); refs = snapshot(api); paths = []
    api.api.cf.CFArrayGetCount.argtypes = [C.c_void_p]
    api.api.cf.CFArrayGetCount.restype = C.c_long
    api.api.cf.CFArrayGetValueAtIndex.argtypes = [C.c_void_p, C.c_long]
    api.api.cf.CFArrayGetValueAtIndex.restype = C.c_void_p
    try:
        for index, ref in enumerate(refs):
            paths.append([str(api.path(api.api.cf.CFArrayGetValueAtIndex(ref, i)))
                          for i in range(api.api.cf.CFArrayGetCount(ref))] if index % 2 else str(api.path(ref)))
        return {'preferences_sha256': hashlib.sha256(json.dumps(paths).encode()).hexdigest()}
    finally:
        for ref in refs: api.api.cf.CFRelease(ref)


def worker(value):
    api = setup()
    api.service_domain()
    with api.opened(Path(value['path'])) as ref:
        api.unlock(ref, value['password'].encode())
        assert api.get(ref, 'synthetic.tianming.probe', 'first') == '合成\0旧引用'.encode()
        api.put(ref, 'synthetic.tianming.probe', 'second', '合成\0新引用'.encode(), os.getuid(), os.getgid())
    return {'old_read': True, 'new_write': True}


def main(value):
    api = setup()
    first = snapshot(api)
    api.service_domain()
    # Metadata and path checks only, no System item lookup or write.
    with api.system():
        pass
    path = Path(value['path'])
    assert path.is_absolute() and not path.exists()
    password = os.urandom(32).hex().encode()
    after_create = after_delete = None
    created = False
    report = {}
    try:
        with api.created(path, password) as ref:
            created = True
            after_create = snapshot(api)
            assert equal(api, first, after_create)
            with _keychain_file(path, os.getuid()):
                api.put(ref, 'synthetic.tianming.probe', 'first', '合成\0旧引用'.encode(), os.getuid(), os.getgid())
                assert api.get(ref, 'synthetic.tianming.probe', 'first') == '合成\0旧引用'.encode()
            try:
                api.put(ref, 'synthetic.tianming.probe', 'first', b'forbidden-overwrite', os.getuid(), os.getgid())
            except ValueError:
                pass
            else:
                raise AssertionError('duplicate was not refused')
            api.api.check(api.api.sec.SecKeychainLock(ref))
            try:
                api.unlock(ref, b'0' * 64)
            except ValueError:
                pass
            else:
                raise AssertionError('wrong password on locked synthetic keychain accepted')
            api.unlock(ref, password)
            assert api.get(ref, 'synthetic.tianming.probe', 'first') == '合成\0旧引用'.encode()
            payload = json.dumps({'worker': True, 'path': str(path), 'password': password.decode()})
            child = subprocess.run(['/usr/bin/python3', '-I', str(Path(__file__).resolve())],
                                   input=payload, capture_output=True, text=True, timeout=30)
            if child.returncode:
                raise ValueError('synthetic second-interpreter probe failed')
            assert json.loads(child.stdout) == {'old_read': True, 'new_write': True}
            assert api.get(ref, 'synthetic.tianming.probe', 'second') == '合成\0新引用'.encode()
            displaced = path.with_name('displaced.keychain-db')
            path.rename(displaced)
            original_add = api.api.sec.SecItemAdd
            def forbidden_add(*args):
                raise AssertionError('missing private file reached native Add')
            api.api.sec.SecItemAdd = forbidden_add
            try:
                try:
                    api.put(ref, 'synthetic.tianming.probe', 'missing-file', b'never-written', os.getuid(), os.getgid())
                except (ValueError, OSError):
                    pass
                else:
                    raise AssertionError('missing private file was not refused before Add')
            finally:
                api.api.sec.SecItemAdd = original_add
                displaced.rename(path)
            report.update(uid_acl_same_uid_other_interpreter=True, rotation_preserves_old=True,
                          duplicate_refused=True, locked_wrong_password_refused=True,
                          private_file_permissions_verified=True, during_creation_preferences_unchanged=True,
                          missing_private_file_refused=True)
    finally:
        if created:
            # Delete only the exact private file successfully created above.
            with api.opened(path) as ref:
                api.api.check(api.api.sec.SecKeychainDelete(ref))
        after_delete = snapshot(api)
        report['after_deletion_preferences_unchanged'] = equal(api, first, after_delete)
        for ref in first + (after_create or []) + after_delete:
            api.api.cf.CFRelease(ref)
    assert report['after_deletion_preferences_unchanged']
    report.update(system_item_read=False, system_item_write=False,
                  dedicated_service_uid_tested=False, other_uid_rejection_tested=False)
    return report


if __name__ == '__main__':
    request = json.loads(sys.stdin.read())
    try:
        print(json.dumps(fingerprint() if request.get('fingerprint_only') else
                         worker(request) if request.get('worker') else main(request)))
    except Exception as error:
        # Synthetic-only exceptions may report safe API status, never input/raw.
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
