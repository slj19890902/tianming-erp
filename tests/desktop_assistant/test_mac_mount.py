"""Synthetic NFS metadata plus read-only native ABI checks; never a NAS approval."""
from copy import deepcopy
import ctypes
import json
import os
import platform
import subprocess
import sys
from types import SimpleNamespace

import pytest
from desktop_assistant import mac_mount as m


def kernel():
    return dict(fsid=[17, 31], filesystem='nfs', mountpoint='/Library/TianmingERP/archive',
                source='nas.example.invalid:/ERP-Archive', owner=0, flags=0x4000018, subtype=0)


def document(k=None):
    k = k or kernel()
    return {k['source']: {'Mount Point': k['mountpoint'], 'Current mount parameters': {
        'General mount flags': {'Bitmask': hex(k['flags']), 'Flags': ['nodev', 'nosuid']},
        'File system locations': [{'Export': '/ERP-Archive', 'Locations': ['192.0.2.20'],
                                  'Server': 'nas.example.invalid'}],
        'NFS parameters': ['vers=4.0', 'tcp', 'port=2049', 'hard', 'nointr', 'sec=sys']},
        'Status flags': {'Bitmask': '0x0', 'Flags': []}}}


def parse(doc): return m.parse_nfs(json.dumps(doc).encode(), kernel())


def test_current_parameters_are_required_not_original_options():
    doc = document(); record = next(iter(doc.values()))
    record['Original mount options'] = {'NFS parameters': ['vers=3', 'udp', 'port=52100']}
    assert parse(doc) == dict(addresses=['192.0.2.20'], export='/ERP-Archive',
        server='nas.example.invalid', version='4.0', port=2049, security='sys', tcp=True)
    del record['Current mount parameters']
    with pytest.raises(KeyError): parse(doc)


@pytest.mark.parametrize('extra', ['tcp', 'udp', 'proto=udp', 'proto=tcp', 'tcp6', 'inet6',
                                    'tcp=1', 'vers=4.0', 'port=2049', 'sec=sys', 'hard'])
def test_duplicate_and_conflicting_transport_is_rejected(extra):
    doc = document(); next(iter(doc.values()))['Current mount parameters']['NFS parameters'].append(extra)
    with pytest.raises(ValueError): parse(doc)


@pytest.mark.parametrize('change', ['missing', 'badport', 'multi', 'hostname', 'mount', 'source', 'flags', 'status'])
def test_incomplete_or_conflicting_nfs_metadata(change):
    doc = document(); record = next(iter(doc.values())); current = record['Current mount parameters']
    if change == 'missing': current['NFS parameters'].remove('sec=sys')
    if change == 'badport': current['NFS parameters'][2] = 'port=65536'
    if change == 'multi': current['File system locations'] *= 2
    if change == 'hostname': current['File system locations'][0]['Locations'] = ['nas.example.invalid']
    if change == 'mount': record['Mount Point'] += '-other'
    if change == 'source': doc['other'] = doc.pop(kernel()['source'])
    if change == 'flags': current['General mount flags']['Bitmask'] = '0x0'
    if change == 'status': record['Status flags']['Bitmask'] = '0x1'
    with pytest.raises(ValueError): parse(doc)


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'])
def test_ambiguous_json_never_used(raw):
    with pytest.raises(ValueError): m.decode(raw)


@pytest.mark.parametrize('address', ['127.0.0.1', '127.5.6.7', '::1', '::ffff:127.0.0.1'])
def test_actual_loopback_location_is_classified_as_proxy(monkeypatch, address):
    doc = document(); next(iter(doc.values()))['Current mount parameters']['File system locations'][0]['Locations'] = [address]
    monkeypatch.setattr(m, '_select', lambda path: kernel())
    monkeypatch.setattr(m.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(doc).encode()))
    assert m.observe('/Library/TianmingERP/archive/backups')['classification'] == 'loopback_proxy'


@pytest.mark.parametrize('kind', ['local', 'smb', 'queryfail', 'timeout', 'changed', 'badjson', 'oversize'])
def test_observation_fails_closed_without_filesystem_io(monkeypatch, kind):
    before = kernel(); after = deepcopy(before)
    if kind == 'local': before.update(filesystem='apfs', flags=0x1000)
    if kind == 'smb': before['filesystem'] = 'smbfs'
    if kind == 'changed': after['fsid'] = [18, 31]
    values = iter([before, after])
    monkeypatch.setattr(m, '_select', lambda path: next(values))
    def run(*a, **kw):
        assert kw['timeout'] == 5 and a[0][:4] == ['/usr/bin/nfsstat', '-f', 'JSON', '-m']
        if kind == 'timeout': raise subprocess.TimeoutExpired(a[0], 5)
        return SimpleNamespace(returncode=1 if kind == 'queryfail' else 0,
            stdout=b'{' if kind == 'badjson' else b'x'*65537 if kind == 'oversize' else json.dumps(document()).encode())
    monkeypatch.setattr(m.subprocess, 'run', run)
    assert m.observe('/Library/TianmingERP/archive/backups')['classification'] == ('local_or_unmounted' if kind == 'local' else 'unknown')


def test_direct_observation_is_still_not_approval(monkeypatch):
    monkeypatch.setattr(m, '_select', lambda path: kernel())
    monkeypatch.setattr(m.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(document()).encode()))
    assert m.observe('/Library/TianmingERP/archive/backups')['classification'] == 'direct_network_unbound'


@pytest.mark.skipif(sys.platform != 'darwin' or platform.machine() != 'arm64', reason='native Darwin arm64 ABI only')
def test_native_local_fd_and_cached_mount_snapshot(tmp_path):
    assert ctypes.sizeof(m.Statfs) == 2168 and ctypes.alignment(m.Statfs) == 8
    assert m.Statfs.fstype.offset == 72 and m.Statfs.source.offset == 1112
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        observed = m.fd_identity(fd)
        assert observed['filesystem'] == 'apfs' and len(observed['fsid']) == 2
    finally: os.close(fd)
    assert m.observe(tmp_path)['classification'] == 'local_or_unmounted'
