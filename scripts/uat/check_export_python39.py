"""Run the real protected-file reader on synthetic files with system Python.

No production paths, credentials, service actions, or authentication are used.
Run with -I -S -B; the explicit source root is this checkout only.
"""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desktop_assistant import uat_export_contract as c
from desktop_assistant import mac_uat_export as broker


def main():
    passed = []
    with tempfile.TemporaryDirectory(prefix='erp-export-python-compat-') as name:
        root = Path(name).resolve()
        source = root/'source'
        source.write_bytes(b'synthetic fixed data')
        source.chmod(0o600)
        assert c.read(source, uid=os.getuid()) == b'synthetic fixed data'
        passed.append('real_reader_with_real_acl_and_parent_checks')

        def denied(label, action):
            try: action()
            except (ValueError, OSError): passed.append(label)
            else: raise AssertionError(label+' was incorrectly accepted')

        denied('size_bound',lambda:c.read(source,uid=os.getuid(),limit=2))
        denied('wrong_owner',lambda:c.read(source,uid=os.getuid()+1))
        source.chmod(0o620)
        denied('writable_file',lambda:c.read(source,uid=os.getuid()))
        source.chmod(0o600)
        hard = root/'hard'; os.link(source,hard)
        denied('hard_link',lambda:c.read(source,uid=os.getuid()))
        hard.unlink()
        link = root/'link'; link.symlink_to(source)
        denied('file_symlink',lambda:c.read(link,uid=os.getuid()))
        link.unlink()
        child = root/'parent'; child.mkdir(); nested = child/'source'
        nested.write_bytes(b'x');nested.chmod(0o600)
        alias = root/'alias';alias.symlink_to(child,target_is_directory=True)
        denied('parent_symlink',lambda:c.read(alias/'source',uid=os.getuid()))

        def change_file():
            with c.regular(source,uid=os.getuid()) as (fd,_):
                assert os.read(fd,100)
                source.write_bytes(b'different data')
        denied('content_changed_before_close',change_file)

        def replace_file():
            with c.regular(source,uid=os.getuid()):
                source.rename(root/'prior');source.write_bytes(b'new');source.chmod(0o600)
        denied('file_replaced_before_close',replace_file)

        def replace_parent():
            with c.regular(nested,uid=os.getuid()):
                child.rename(root/'prior-parent');child.mkdir()
                nested.write_bytes(b'new');nested.chmod(0o600)
        denied('parent_replaced_before_close',replace_parent)

        lock = root/'lock';lock.write_bytes(b'0');lock.chmod(0o600)
        with c.regular(lock,uid=os.getuid(),limit=1) as (fd,_):
            import fcntl
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            assert os.read(fd,2)==b'0'
            fcntl.flock(fd,fcntl.LOCK_UN)
        passed.append('real_existing_lock_reader')

        # Only the fixed destination/recipient admission is replaced with this
        # synthetic location. FD traversal and lstat rechecks are the real code.
        out=root/'output';out.mkdir()
        prior=(c.OUTPUT,c.recipient,broker._directory_ok)
        c.OUTPUT=out;c.recipient=lambda:{'synthetic':True}
        broker._directory_ok=lambda fd,p:None
        try:
            with broker.output_parent() as (fd,check):
                assert os.fstat(fd).st_ino==out.lstat().st_ino
                check()
            passed.append('destination_parent_real_path_api')
            def replace_output():
                with broker.output_parent():
                    out.rename(root/'prior-output');out.mkdir()
            denied('destination_parent_replaced',replace_output)
        finally:
            c.OUTPUT,c.recipient,broker._directory_ok=prior
    print(json.dumps({'python':list(sys.version_info[:3]),'passed':passed,
        'count':len(passed),'production_io':False,'credentials':False}))


if __name__=='__main__': main()
