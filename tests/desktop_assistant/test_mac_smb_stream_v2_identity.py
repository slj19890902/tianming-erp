"""Creation-time aliases are permitted; held identity/content gates remain."""
import pytest
import hashlib
import os
from desktop_assistant import mac_smb_stream_probe_v2 as probe
from desktop_assistant import mac_smb_stream_probe as legacy
from test_mac_smb_stream_probe import Peer,ATTEMPT,contract

def aliases(peer):
    def hook(cmd,node,result):
        if cmd==9:
            for current in (node,peer.nodes['BoxERP\\mac-server-backups']):
                current['created']+=100
                current['write_time']=current['created']
                current['change_time']=current['created']
    peer.hook=hook

def test_virtual_nas_timestamp_alias_is_not_object_replacement():
    peer=Peer();aliases(peer)
    proof=probe.check(peer.tree(),contract(),ATTEMPT,16384)
    assert proof['type']=='tianming.mac-smb-stream-preflight.v2'
    assert proof['canary_removed'] is True and set(proof['file'])=={'index'}
    assert set(peer.nodes)==set(probe.files.DIRECTORIES)

def test_original_v1_policy_is_unchanged():
    peer=Peer();aliases(peer)
    with pytest.raises(ValueError):legacy.check(peer.tree(),contract(),ATTEMPT,16384)
    assert not any(kind=='delete-held' for kind,name in peer.events)

@pytest.mark.parametrize('fault',['file-index','directory-index','contents','size','reparse','hardlink'])
def test_v2_rejects_replacement_or_content_changes_without_cleanup(fault):
    peer=Peer()
    def hook(cmd,node,result):
        if cmd==9:
            if fault=='file-index':node['index']+=1
            if fault=='directory-index':peer.nodes['BoxERP\\mac-server-backups']['index']+=1
            if fault=='size':node['data'].extend(b'x')
            if fault=='reparse':node['attrs']|=0x400
            if fault=='hardlink':node['links']=2
        if cmd==8 and fault=='contents':result['buffer']=b'wrong'
    peer.hook=hook
    with pytest.raises(ValueError):probe.check(peer.tree(),contract(),ATTEMPT,16384)
    assert not any(kind=='delete-held' for kind,name in peer.events)

def test_changed_held_file_id_rejected_before_network_query():
    peer=Peer();archive=probe.Probe(peer.tree(),contract());archive.open_directories()
    handle,info=archive._open('BoxERP\\mac-server-backups\\held-canary.partial',create=True)
    before=len(peer.requests);original=handle.file_id
    try:
        handle.file_id=b'z'*16
        with pytest.raises(ValueError):probe.files._query(handle)
        assert len(peer.requests)==before
    finally:
        handle.file_id=original;handle.close();archive.close()

@pytest.mark.skipif(os.name!='posix',reason='Local backup descriptor uses POSIX fcntl; SMB identity cases remain portable')
def test_v2_full_independent_readback_with_timestamp_aliases(tmp_path):
    peer=Peer();aliases(peer)
    data=probe.files.MAGIC+os.urandom(65536)
    source=tmp_path/'source';source.write_bytes(data);source.chmod(0o600)
    destination=tmp_path/'destination';destination.touch(mode=0o600)
    filename='20261012-120000-12345678.tmbackup'
    digest=hashlib.sha256(data).hexdigest()
    source_fd=os.open(source,os.O_RDONLY);destination_fd=os.open(destination,os.O_RDWR)
    try:
        with probe.files.open_archive(peer.tree(),contract()) as archive:
            uploaded=archive.upload(source_fd,filename,len(data),digest,ATTEMPT)
        with probe.files.open_archive(peer.tree(),contract()) as archive:
            downloaded=archive.download(destination_fd,uploaded)
        assert uploaded['connection_guid']!=downloaded['connection_guid']
        assert uploaded['type'].endswith('.v2') and downloaded['type'].endswith('.v2')
        assert destination.read_bytes()==data
        assert downloaded['backup_authenticated'] is False
    finally:os.close(source_fd);os.close(destination_fd)
