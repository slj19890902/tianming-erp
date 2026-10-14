"""All input files are synthetic; never invoke the collector's formal default."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from scripts.audit import collect_map_binding_source as collector


def name(stamp='20261014_120000_000001', prefix='000000000000'):
    return f'twin_layout_v1.before_{stamp}_{prefix}.json'


def known(monkeypatch, content=b'{"floors":{"1F":{"revision":"original"},"3F":{"raw":10.0}}}\r\n'):
    monkeypatch.setattr(collector, 'EXPECTED_SHA256', sha256(content).hexdigest())
    return content


def test_default_contract_is_fixed_and_has_no_business_imports():
    assert collector.EXPECTED_SHA256=='e79feaba1ab0443a1ef7a337a10e7d3615a8536d21298a0af20161a390da521b'
    assert collector.ALLOWED_DATES=={'20261013','20261014'}
    assert (collector.MAX_FILES,collector.MAX_FILE_BYTES,collector.MAX_TOTAL_BYTES)==(8,2*1024**2,16*1024**2)
    assert (collector.MAX_ENTRIES,collector.ENUMERATION_SECONDS,collector.TOTAL_SECONDS)==(4096,3,20)


def test_exact_raw_bytes_preserved_and_hash_prefix_precedes_newer_filename(tmp_path,monkeypatch):
    content=known(monkeypatch)
    old=name('20261013_030237_000001',collector.EXPECTED_SHA256[:12])
    (tmp_path/old).write_bytes(content)
    (tmp_path/name()).write_bytes(b'unrelated newest')
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    handler=signal.getsignal(signal.SIGALRM)
    report,payload=collector.collect(tmp_path)
    assert report['status']=='found' and payload==content
    assert report['files_opened']==1 and report['match']['name']==old
    assert report['checked'][0]['identity_before']==report['checked'][0]['identity_after']
    assert report['directory_identity_unchanged'] and not report['files_written']
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert signal.getsignal(signal.SIGALRM)==handler and signal.getitimer(signal.ITIMER_REAL)==(0.0,0.0)
    assert len(json.dumps(report))<8000


def test_prefix_is_not_proof_and_other_candidates_are_newest_first(tmp_path,monkeypatch):
    content=known(monkeypatch)
    forged=name('20261013_010000_000001',collector.EXPECTED_SHA256[:12])
    newest=name('20261014_120000_000002')
    older=name('20261013_120000_000002')
    (tmp_path/forged).write_bytes(b'wrong bytes despite right prefix')
    (tmp_path/newest).write_bytes(b'unrelated')
    (tmp_path/older).write_bytes(content)
    report,payload=collector.collect(tmp_path)
    assert payload==content and [c['name'] for c in report['checked']]==[forged,newest,older]


def test_no_recursion_symlinks_nonregular_or_out_of_scope_reads(tmp_path,monkeypatch):
    content=known(monkeypatch)
    nested=tmp_path/name();nested.mkdir();(nested/name()).write_bytes(content)
    outside=tmp_path/'outside.txt';outside.write_bytes(content)
    (tmp_path/name('20261014_120000_000003')).symlink_to(outside)
    os.mkfifo(tmp_path/name('20261014_120000_000004'))
    (tmp_path/name('20261012_120000_000005')).write_bytes(content)
    (tmp_path/name('20261014_250000_000006')).write_bytes(content)
    monkeypatch.setattr(collector.os,'read',lambda *a: pytest.fail('scope rejection must not read any content'))
    report,payload=collector.collect(tmp_path)
    assert report['status']=='not_found_within_scope' and payload is None
    assert report['files_opened']==0 and report['bytes_read']==0
    assert report['ignored_counts']['symlink_or_nonregular']==3


@pytest.mark.parametrize('kind',['final_symlink','ancestor_symlink','relative','parent_traversal','missing'])
def test_unsafe_or_missing_directory_is_rejected_without_fallback(tmp_path,kind):
    real=tmp_path/'real';real.mkdir();(real/'nested').mkdir()
    link=tmp_path/'link';link.symlink_to(real,target_is_directory=True)
    path={'final_symlink':link,'ancestor_symlink':link/'nested','relative':'relative',
          'parent_traversal':str(real)+'/../real','missing':tmp_path/'missing'}[kind]
    report,payload=collector.collect(path)
    assert report['status']=='rejected' and payload is None and report['files_opened']==0


def test_file_count_limit_does_not_read_ninth_matching_candidate(tmp_path,monkeypatch):
    content=known(monkeypatch)
    for number in range(1,10):
        (tmp_path/name(f'20261014_120000_{number:06}')).write_bytes(content if number==1 else b'not it')
    report,payload=collector.collect(tmp_path)
    assert (report['status'],report['reason'],report['files_opened'])==('limit_reached','files',8)
    assert payload is None and report['enumeration_complete']


def test_file_size_limit_reads_no_content(tmp_path,monkeypatch):
    (tmp_path/name()).write_bytes(b'x'*(collector.MAX_FILE_BYTES+1))
    monkeypatch.setattr(collector.os,'read',lambda *a: pytest.fail('oversize file must not be read'))
    report,payload=collector.collect(tmp_path)
    assert (report['status'],report['reason'])==('limit_reached','file_bytes')
    assert report['bytes_read']==0 and payload is None


def test_total_byte_limit_stops_before_next_file(tmp_path,monkeypatch):
    monkeypatch.setattr(collector,'MAX_TOTAL_BYTES',5)
    for number in range(3): (tmp_path/name(f'20261014_120000_{number:06}')).write_bytes(b'123')
    report,payload=collector.collect(tmp_path)
    assert (report['status'],report['reason'],report['bytes_read'])==('limit_reached','total_bytes',3)
    assert report['files_opened']==1 and payload is None


def test_directory_limit_is_incomplete_not_not_found(tmp_path,monkeypatch):
    monkeypatch.setattr(collector,'MAX_ENTRIES',2)
    for number in range(3):(tmp_path/f'ignored-{number}').write_bytes(b'x')
    report,payload=collector.collect(tmp_path)
    assert (report['status'],report['reason'],report['entries_seen'])==('limit_reached','directory_entries',2)
    assert report['enumeration_incomplete'] and report['files_opened']==0 and payload is None


@pytest.mark.parametrize('kind',['rewrite','replace','truncate','symlink'])
def test_changed_candidate_never_returns_bytes_even_if_original_sha_matched(tmp_path,monkeypatch,kind):
    content=known(monkeypatch);path=tmp_path/name();path.write_bytes(content)
    original=collector.os.read;done=False
    def changed(fd,size):
        nonlocal done
        block=original(fd,size)
        if not done:
            done=True
            if kind=='rewrite': path.write_bytes(b'x'*len(content))
            elif kind=='truncate': path.write_bytes(b'')
            elif kind=='replace':
                new=tmp_path/'replacement';new.write_bytes(content);os.replace(new,path)
            else:
                path.unlink();path.symlink_to(tmp_path/'absent')
        return block
    monkeypatch.setattr(collector.os,'read',changed)
    report,payload=collector.collect(tmp_path)
    assert report['status']=='rejected' and report['reason']=='candidate_changed_during_read'
    assert report['match'] is None and payload is None


def test_candidate_replacement_between_stat_and_open_is_rejected(tmp_path,monkeypatch):
    content=known(monkeypatch);path=tmp_path/name();path.write_bytes(content)
    original=collector.os.open
    def changed(p,flags,*args,**kwargs):
        if p==path.name:
            new=tmp_path/'new';new.write_bytes(content);os.replace(new,path)
        return original(p,flags,*args,**kwargs)
    monkeypatch.setattr(collector.os,'open',changed)
    report,payload=collector.collect(tmp_path)
    assert report['status']=='rejected' and report['reason']=='candidate_changed_at_open' and payload is None


def test_directory_replacement_discards_an_already_matched_file(tmp_path,monkeypatch):
    root=tmp_path/'input';root.mkdir();content=known(monkeypatch);(root/name()).write_bytes(content)
    original=collector.os.read;done=False
    def changed(fd,size):
        nonlocal done
        block=original(fd,size)
        if not done:
            done=True;root.rename(tmp_path/'old');root.mkdir();(root/name()).write_bytes(content)
        return block
    monkeypatch.setattr(collector.os,'read',changed)
    report,payload=collector.collect(root)
    assert report['status']=='rejected' and report['reason']=='directory_changed_during_collection'
    assert report['match'] is None and payload is None


@pytest.mark.parametrize('phase',['enumeration','read'])
def test_alarm_interrupts_blocking_operation_and_restores_signal_handler(tmp_path,monkeypatch,phase):
    monkeypatch.setattr(collector,'TOTAL_SECONDS',0.08)
    monkeypatch.setattr(collector,'ENUMERATION_SECONDS',0.04)
    (tmp_path/name()).write_bytes(b'not it')
    original=collector.os.scandir if phase=='enumeration' else collector.os.read
    def slow(*a,**k):time.sleep(1);return original(*a,**k)
    monkeypatch.setattr(collector.os,'scandir' if phase=='enumeration' else 'read',slow)
    before=time.monotonic();handler=signal.getsignal(signal.SIGALRM)
    report,payload=collector.collect(tmp_path)
    assert time.monotonic()-before<0.5
    assert report['status']=='limit_reached' and report['reason']==('enumeration_seconds' if phase=='enumeration' else 'total_seconds')
    assert payload is None and signal.getsignal(signal.SIGALRM)==handler
    assert signal.getitimer(signal.ITIMER_REAL)==(0.0,0.0)


def test_existing_alarm_is_left_running(tmp_path):
    handler=signal.getsignal(signal.SIGALRM)
    signal.setitimer(signal.ITIMER_REAL,5)
    try:
        report,payload=collector.collect(tmp_path)
        assert report['reason']=='existing_alarm_not_modified' and payload is None
        assert signal.getsignal(signal.SIGALRM)==handler and signal.getitimer(signal.ITIMER_REAL)[0]>4
    finally:signal.setitimer(signal.ITIMER_REAL,0)


def test_non_main_thread_declines_before_filesystem_access(tmp_path,monkeypatch):
    monkeypatch.setattr(collector,'_directory',lambda *a: pytest.fail('must decline before filesystem access'))
    with ThreadPoolExecutor(max_workers=1) as worker: report,payload=worker.submit(collector.collect,tmp_path).result()
    assert report['reason']=='main_thread_required_for_hard_deadline' and payload is None


def test_stdlib_cli_only_runs_against_explicit_synthetic_directory_and_writes_nothing(tmp_path):
    result=subprocess.run([sys.executable,'-B','-S',collector.__file__,'--directory',str(tmp_path),'--include-bytes'],
                          capture_output=True,text=True,timeout=3)
    assert result.returncode==2
    payload=json.loads(result.stdout)
    assert payload['manifest']['status']=='not_found_within_scope'
    assert payload['matching_bytes_base64'] is None and list(tmp_path.iterdir())==[]
