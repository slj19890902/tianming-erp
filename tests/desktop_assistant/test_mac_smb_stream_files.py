"""Actual pinned SMB structures/Open against a deterministic in-memory peer.

No NAS credentials or network. Local files and recovery encryption are real.
"""
import hashlib
import os
import struct
import types
import uuid

import pytest

pytest.importorskip('smbprotocol')
from smbprotocol import open as smb
from smbprotocol.file_info import FileAllInformation, FileRenameInformation
from smbprotocol.header import SMB2HeaderResponse, SMB2HeaderRequest

from desktop_assistant import mac_smb_stream_files as subject
from desktop_assistant.mac_smb_stream_session import _connection_class
from desktop_assistant.storage import encrypt_file, decrypt_file, MAGIC

pytestmark=pytest.mark.skipif(os.name!='posix',reason='POSIX private FD transfer component')
NAME='20261012-010203-abcdef01.tmbackup'
FINAL='BoxERP\\mac-server-backups\\'+NAME
ATTEMPT='d'*32
PARTIAL=FINAL+'.'+ATTEMPT+'.partial'


def contract():
    return {'type':'tianming.mac-smb-stream-archive.v1','context_sha256':'a'*64,
        'uid':499,'server_address':'192.168.1.245','port':445,'username':'backup_admin',
        'share':'备份','relative_directory':'BoxERP/mac-server-backups',
        'server_guid':'b'*32,'protection':'smb311-session-aes-gcm'}


class Peer:
    def __init__(self):
        self.nodes={};self.handles={};self.serial=100;self.events=[];self.requests=[]
        self.write_limit=7777;self.read_limit=5555;self.hook=lambda *args:None
        for name in subject.DIRECTORIES:self.new_node(name,True)

    def new_node(self,name,directory=False,data=b''):
        self.serial+=1
        node=dict(name=name,index=self.serial,created=133000000000000000+self.serial,
            attrs=0x10 if directory else 0x80,directory=directory,data=bytearray(data),
            links=1,delete_pending=False,write_time=11,change_time=22)
        self.nodes[name]=node;return node

    def tree(self):
        peer=self
        class Base:
            def send(self,message,sid=None,tid=None):
                assert sid==17 and tid==23
                # Pack and unpack every request, exercising the real wire layouts.
                parsed=type(message)();assert parsed.unpack(message.pack())==b''
                header=SMB2HeaderRequest();header['data']=message.pack()
                return types.SimpleNamespace(message=header,payload=parsed)
            def receive(self,request,**kwargs):
                assert kwargs['resolve_symlinks'] is False
                assert 0<kwargs['timeout']<=30
                return peer.reply(request.payload)
        conn=_connection_class(Base,30)()
        conn.handshake_done=True;conn.dialect=0x311;conn.cipher_id=2
        conn.preauth_integrity_hash_id=1;conn.supports_encryption=True;conn.require_signing=True
        conn.server_guid=uuid.UUID(hex='b'*32);conn.client_guid=uuid.uuid4()
        conn.max_write_size=65536;conn.max_read_size=65536
        session=types.SimpleNamespace(connection=conn,_connected=True,require_encryption=True,
            encrypt_data=True,auth_protocol='ntlm',username='backup_admin',session_id=17,
            session_key=b'k'*16,signing_key=b's'*16,encryption_key=b'e'*16,
            decryption_key=b'd'*16,tree_connect_table={},open_table={})
        conn.session_table={17:session}
        tree=types.SimpleNamespace(session=session,_connected=True,is_dfs_share=False,
            share_name='\\\\192.168.1.245\\备份',tree_connect_id=23)
        session.tree_connect_table[23]=tree
        return tree

    def reply(self,request):
        cmd=request.COMMAND;self.requests.append(request)
        node=None
        if cmd==5:
            name=request['buffer_path'].get_value().decode('utf-16-le').rstrip('\0')
            self.events.append(('open',name))
            create=request['create_disposition'].get_value()==2
            assert request['create_disposition'].get_value() in (1,2)
            assert request['create_options'].get_value() & 0x200000
            directory=bool(request['create_options'].get_value() & 1)
            assert request['share_access'].get_value()==(3 if directory else 0 if create else 1)
            assert request['requested_oplock_level'].get_value()==0
            if create:
                if name in self.nodes:raise FileExistsError('synthetic collision')
                assert not directory and name.endswith('.partial')
                node=self.new_node(name)
            else:node=self.nodes[name]
            self.serial+=1;fid=struct.pack('<QQ',self.serial,1);self.handles[fid]=node
            result=smb.SMB2CreateResponse();result['create_action']=2 if create else 1
            result['file_id']=fid;result['file_attributes']=node['attrs']
            result['end_of_file']=len(node['data'])
        else:
            fid=request['file_id'].get_value();node=self.handles[fid]
            if cmd==16:
                assert request['info_type'].get_value()==FileAllInformation.INFO_TYPE
                assert request['file_info_class'].get_value()==FileAllInformation.INFO_CLASS
                info=FileAllInformation()
                basic=info['basic_information'].get_value()
                basic['creation_time']=node['created'];basic['file_attributes']=node['attrs']
                basic['last_write_time']=node['write_time'];basic['change_time']=node['change_time']
                standard=info['standard_information'].get_value()
                standard['end_of_file']=len(node['data']);standard['directory']=node['directory']
                standard['number_of_links']=node['links'];standard['delete_pending']=node['delete_pending']
                info['internal_information'].get_value()['index_number']=node['index']
                result=smb.SMB2QueryInfoResponse();result['buffer']=info.pack()
            elif cmd==9:
                assert request['flags'].get_value() & 1
                data=request['buffer'].get_value()[:self.write_limit]
                offset=request['offset'].get_value()
                node['data'][offset:offset+len(data)]=data
                result=smb.SMB2WriteResponse();result['count']=len(data)
                self.events.append(('write',node['name']))
            elif cmd==8:
                offset=request['offset'].get_value();length=request['length'].get_value()
                result=smb.SMB2ReadResponse()
                result['buffer']=bytes(node['data'][offset:offset+min(length,self.read_limit)])
                self.events.append(('read',node['name']))
            elif cmd==7:
                result=smb.SMB2FlushResponse();self.events.append(('flush',node['name']))
            elif cmd==17:
                assert request['file_info_class'].get_value()==FileRenameInformation.INFO_CLASS
                rename=FileRenameInformation();assert rename.unpack(request['buffer'].get_value())==b''
                assert rename['replace_if_exists'].get_value() is False
                assert rename['root_directory'].get_value()==0
                name=rename['file_name'].get_value()
                if name in self.nodes:raise FileExistsError('synthetic final-name collision')
                del self.nodes[node['name']];node['name']=name;self.nodes[name]=node
                result=smb.SMB2SetInfoResponse();self.events.append(('rename',name))
            elif cmd==6:
                result=smb.SMB2CloseResponse();del self.handles[fid]
                self.events.append(('close',node['name']))
            else:raise AssertionError(cmd)
        self.hook(cmd,node,result)
        header=SMB2HeaderResponse();header['command']=cmd;header['session_id']=17
        header['tree_id']=23;header['data']=result.pack()
        return header


@pytest.fixture
def files(tmp_path):
    source=tmp_path/'ciphertext.tmbackup'
    source.write_bytes(MAGIC+b'synthetic ciphertext '*5000);source.chmod(0o600)
    destination=tmp_path/'download.tmbackup'
    return source,destination


def upload(peer,source,**kwargs):
    size=source.stat().st_size;sha=hashlib.sha256(source.read_bytes()).hexdigest()
    with source.open('rb') as stream,subject.open_archive(peer.tree(),contract()) as archive:
        return archive.upload(stream.fileno(),NAME,kwargs.get('size',size),kwargs.get('sha',sha),ATTEMPT)


def download(peer,destination,proof,tree=None):
    with destination.open('x+b') as stream:
        destination.chmod(0o600)
        with subject.open_archive(tree or peer.tree(),contract()) as archive:
            return archive.download(stream.fileno(),proof)


def test_complete_ciphertext_roundtrip_then_original_recovery_auth(tmp_path):
    raw=tmp_path/'recovery.zip';raw.write_bytes(bytes(range(256))*5000)
    cipher=tmp_path/'source.tmbackup';encrypt_file(raw,cipher,'synthetic-backup-password')
    cipher.chmod(0o600)
    peer=Peer();proof=upload(peer,cipher)
    assert proof['type']==subject.UPLOAD and proof['backup_authenticated'] is False
    assert PARTIAL not in peer.nodes and bytes(peer.nodes[FINAL]['data'])==cipher.read_bytes()
    assert not peer.handles
    target=tmp_path/'download.tmbackup';verified=download(peer,target,proof)
    assert target.read_bytes()==cipher.read_bytes()
    assert verified['type']==subject.READBACK and verified['backup_authenticated'] is False
    assert verified['connection_guid']!=proof['connection_guid']
    assert verified['file']==proof['file'] and verified['directories']==proof['directories']
    decrypted=tmp_path/'decrypted.zip';decrypt_file(target,decrypted,'synthetic-backup-password')
    assert decrypted.read_bytes()==raw.read_bytes() and not peer.handles


@pytest.mark.parametrize('existing',[PARTIAL,FINAL])
def test_collisions_never_overwrite_and_partial_retained(files,existing):
    source,_=files;peer=Peer();node=peer.new_node(existing,data=b'original')
    with pytest.raises(ValueError):upload(peer,source)
    assert peer.nodes[existing] is node and bytes(node['data'])==b'original'
    if existing==FINAL:assert bytes(peer.nodes[PARTIAL]['data'])==source.read_bytes()
    assert not peer.handles


@pytest.mark.parametrize('which',[0,1,2])
@pytest.mark.parametrize('failure',['reparse','not_directory','delete_pending','missing'])
def test_unusable_fixed_parent_never_creates_file(files,which,failure):
    source,_=files;peer=Peer();name=subject.DIRECTORIES[which];node=peer.nodes[name]
    if failure=='reparse':node['attrs']|=0x400
    elif failure=='not_directory':node.update(attrs=0x80,directory=False)
    elif failure=='delete_pending':node['delete_pending']=True
    else:del peer.nodes[name]
    with pytest.raises(ValueError):upload(peer,source)
    assert not any(n.endswith('.partial') for n in peer.nodes) and not peer.handles


@pytest.mark.parametrize('cmd',[5,9,7,17,6])
def test_unknown_remote_outcome_is_preserved(files,cmd):
    source,_=files;peer=Peer();triggered=[]
    def hook(actual,node,result):
        if actual==cmd and not node['directory'] and not triggered:
            triggered.append(True);raise OSError('synthetic lost acknowledgement')
    peer.hook=hook
    with pytest.raises(ValueError):upload(peer,source)
    assert triggered and (PARTIAL in peer.nodes or FINAL in peer.nodes)
    # No delete, overwrite, retry-create or automatic resume request.
    creates=[r for r in peer.requests if r.COMMAND==5 and r['create_disposition'].get_value()==2]
    assert len(creates)==1


@pytest.mark.parametrize('count',[0,999999])
def test_invalid_write_count_fails_without_rename(files,count):
    source,_=files;peer=Peer()
    def hook(cmd,node,result):
        if cmd==9:result['count']=count
    peer.hook=hook
    with pytest.raises(ValueError):upload(peer,source)
    assert PARTIAL in peer.nodes and FINAL not in peer.nodes


def test_source_hash_mismatch_keeps_partial(files):
    source,_=files;peer=Peer()
    with pytest.raises(ValueError):upload(peer,source,sha='0'*64)
    assert PARTIAL in peer.nodes and FINAL not in peer.nodes


def test_source_mutation_is_not_published(files):
    source,_=files;peer=Peer();changed=[]
    def hook(cmd,node,result):
        if cmd==9 and not changed:
            with source.open('ab') as f:f.write(b'x')
            changed.append(True)
    peer.hook=hook
    with pytest.raises(ValueError):upload(peer,source)
    assert PARTIAL in peer.nodes and FINAL not in peer.nodes


@pytest.mark.parametrize('fault',['permissions','size','plaintext','hardlink'])
def test_untrusted_local_source_rejected_before_remote_write(files,fault):
    source,_=files;peer=Peer();kwargs={}
    if fault=='permissions':source.chmod(0o644)
    elif fault=='size':kwargs['size']=source.stat().st_size+1
    elif fault=='plaintext':source.write_bytes(b'not ciphertext'*100)
    else:os.link(source,source.with_suffix('.link'))
    with pytest.raises(ValueError):upload(peer,source,**kwargs)
    assert not any(n.endswith('.partial') for n in peer.nodes)


@pytest.mark.parametrize('fault',['file_id','file_created','directory_id','directory_created','size','hash','hardlink','reparse'])
def test_independent_readback_detects_replacement_and_corruption(files,fault):
    source,dest=files;peer=Peer();proof=upload(peer,source);node=peer.nodes[FINAL]
    if fault=='file_id':node['index']+=1
    elif fault=='file_created':node['created']+=1
    elif fault=='directory_id':peer.nodes[subject.DIRECTORIES[2]]['index']+=1
    elif fault=='directory_created':peer.nodes[subject.DIRECTORIES[2]]['created']+=1
    elif fault=='size':node['data']+=b'x'
    elif fault=='hash':node['data'][-1]^=1
    elif fault=='hardlink':node['links']=2
    else:node['attrs']|=0x400
    with pytest.raises(ValueError):download(peer,dest,proof)
    assert FINAL in peer.nodes and dest.exists()


def test_same_connection_readback_rejected_before_file_open(files):
    source,dest=files;peer=Peer();proof=upload(peer,source);tree=peer.tree()
    tree.session.connection.client_guid=uuid.UUID(hex=proof['connection_guid'])
    peer.events.clear()
    with pytest.raises(ValueError):download(peer,dest,proof,tree)
    assert ('open',FINAL) not in peer.events


def test_short_reads_and_local_short_writes_are_completed(files,monkeypatch):
    source,dest=files;peer=Peer();proof=upload(peer,source)
    original=os.pwrite
    monkeypatch.setattr(os,'pwrite',lambda fd,data,offset:original(fd,data[:2000],offset))
    download(peer,dest,proof)
    assert dest.read_bytes()==source.read_bytes()


@pytest.mark.parametrize('fault',['eof','overlong','metadata_change'])
def test_download_interruption_does_not_return_proof(files,fault):
    source,dest=files;peer=Peer();proof=upload(peer,source)
    def hook(cmd,node,result):
        if cmd==8:
            if fault=='eof':result['buffer']=b''
            elif fault=='overlong':result['buffer']=b'x'*70000
            else:node['write_time']+=1
    peer.hook=hook
    with pytest.raises(ValueError):download(peer,dest,proof)
    assert dest.exists() and FINAL in peer.nodes


def test_one_operation_per_archive(files):
    source,_=files;peer=Peer();raw=source.read_bytes()
    with source.open('rb') as stream,subject.open_archive(peer.tree(),contract()) as archive:
        archive.upload(stream.fileno(),NAME,len(raw),hashlib.sha256(raw).hexdigest(),ATTEMPT)
        with pytest.raises(ValueError):
            archive.upload(stream.fileno(),NAME,len(raw),hashlib.sha256(raw).hexdigest(),ATTEMPT)
    assert FINAL in peer.nodes


def test_directory_identity_change_during_write_prevents_publication(files):
    source,_=files;peer=Peer();changed=[]
    def hook(cmd,node,result):
        if cmd==9 and not changed:
            peer.nodes[subject.DIRECTORIES[1]]['created']+=1;changed.append(True)
    peer.hook=hook
    with pytest.raises(ValueError):upload(peer,source)
    assert PARTIAL in peer.nodes and FINAL not in peer.nodes


def test_closed_archive_cannot_claim_same_connection_again():
    peer=Peer();tree=peer.tree()
    with subject.open_archive(tree,contract()):pass
    with pytest.raises(ValueError):
        with subject.open_archive(tree,contract()):pytest.fail('connection reused')


@pytest.mark.parametrize('fault',['nonempty','permissions','hardlink','readonly','append'])
def test_download_requires_new_private_writable_descriptor(files,fault):
    source,dest=files;peer=Peer();proof=upload(peer,source)
    dest.write_bytes(b'existing' if fault=='nonempty' else b'');dest.chmod(0o600)
    if fault=='permissions':dest.chmod(0o644)
    if fault=='hardlink':os.link(dest,dest.with_suffix('.link'))
    flags=os.O_RDONLY if fault=='readonly' else os.O_RDWR
    if fault=='append':flags|=os.O_APPEND
    fd=os.open(dest,flags)
    peer.events.clear()
    try:
        with pytest.raises(ValueError):
            with subject.open_archive(peer.tree(),contract()) as archive:archive.download(fd,proof)
    finally:os.close(fd)
    assert ('open',FINAL) not in peer.events
    assert dest.read_bytes()==(b'existing' if fault=='nonempty' else b'')


def test_local_destination_bytes_independently_hashed_after_writes(files,monkeypatch):
    source,dest=files;peer=Peer();proof=upload(peer,source)
    original=os.pwrite
    def corrupt(fd,data,offset):
        damaged=bytearray(data);damaged[-1]^=1
        return original(fd,damaged,offset)
    monkeypatch.setattr(os,'pwrite',corrupt)
    with pytest.raises(ValueError):download(peer,dest,proof)
    assert dest.exists() and FINAL in peer.nodes


@pytest.mark.parametrize('fault',['zero_index','zero_created','bad_action','invalid_file_id','directory_file'])
def test_new_file_must_have_real_identity_and_be_regular(files,fault):
    source,_=files;peer=Peer()
    def hook(cmd,node,result):
        if cmd==5 and not node['directory']:
            if fault=='zero_index':node['index']=0
            elif fault=='zero_created':node['created']=0
            elif fault=='bad_action':result['create_action']=1
            elif fault=='invalid_file_id':result['file_id']=b'\xff'*16
            else:node['directory']=True;node['attrs']=0x10
    peer.hook=hook
    with pytest.raises(ValueError):upload(peer,source)
    assert PARTIAL in peer.nodes and FINAL not in peer.nodes
    assert not any(event[0]=='write' for event in peer.events)


def test_directory_close_failure_invalidates_otherwise_finished_transfer(files):
    source,_=files;peer=Peer();once=[]
    def hook(cmd,node,result):
        if cmd==6 and node['directory'] and not once:
            once.append(True);raise OSError('synthetic missing close ack')
    peer.hook=hook
    with pytest.raises(ValueError):upload(peer,source)
    assert FINAL in peer.nodes


def test_written_file_cannot_turn_into_directory_before_rename(files):
    source,_=files;peer=Peer()
    def hook(cmd,node,result):
        if cmd==7:node['directory']=True;node['attrs']=0x10
    peer.hook=hook
    with pytest.raises(ValueError):upload(peer,source)
    assert PARTIAL in peer.nodes and FINAL not in peer.nodes
