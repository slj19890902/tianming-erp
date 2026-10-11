"""No-overwrite encrypted backup transfer over an already verified SMB tree.

No sockets, credentials, directory creation, deletion or success-state writes.
Transfer observations are not full backup authentication. The signed worker must
bind these results to its root-approved plan, then authenticate the entire
download with the original recovery verifier before publishing success.
"""
from contextlib import contextmanager
import hashlib
import os
import re
import stat

from . import mac_smb_stream_contract as contract
from .mac_smb_stream_session import _authenticated
from .storage import MAGIC

ERROR = 'SMB完整文件传输未确认；保留本地和远端现场，不覆盖或重试'
UPLOAD = 'tianming.mac-smb-stream-upload.v1'
READBACK = 'tianming.mac-smb-stream-download.v1'
CHUNK = 65536  # One SMB credit; no dependence on multichannel/large-MTU grants.
DIRECTORIES = ('','BoxERP','BoxERP\\mac-server-backups')


def require(ok):
    if not ok: raise ValueError(ERROR)


def _spec(size, sha256):
    require(type(size) is int and len(MAGIC)+44 <= size < 2**63
            and isinstance(sha256,str) and re.fullmatch('[0-9a-f]{64}',sha256))


def _local(fd, *, writable):
    import fcntl
    require(type(fd) is int and fd >= 3)
    s=os.fstat(fd)
    require(stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o600
            and s.st_uid==os.geteuid() and s.st_nlink==1)
    flags=fcntl.fcntl(fd,fcntl.F_GETFL)
    require(flags & os.O_ACCMODE == (os.O_RDWR if writable else os.O_RDONLY)
            and not flags & os.O_APPEND)
    identity=(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink)
    return identity,(s.st_size,s.st_mtime_ns,s.st_ctime_ns)


def _query(handle):
    from smbprotocol.open import SMB2QueryInfoRequest, SMB2QueryInfoResponse
    from smbprotocol.file_info import FileAllInformation
    request=SMB2QueryInfoRequest()
    request['info_type']=FileAllInformation.INFO_TYPE
    request['file_info_class']=FileAllInformation.INFO_CLASS
    request['output_buffer_length']=4096
    request['file_id']=handle.file_id
    tree=handle.tree_connect
    sent=handle.connection.send(request,sid=tree.session.session_id,tid=tree.tree_connect_id)
    response=handle.connection.receive(sent,resolve_symlinks=False)
    wrapper=SMB2QueryInfoResponse()
    require(wrapper.unpack(response['data'].get_value()) == b'')
    raw=wrapper['buffer'].get_value()
    require(len(raw)<=4096)
    info=FileAllInformation()
    require(info.unpack(raw)==b'')
    basic=info['basic_information'].get_value()
    standard=info['standard_information'].get_value()
    result={
        'index':info['internal_information'].get_value()['index_number'].get_value(),
        'created':basic['creation_time'].get_value(),
        'write_time':basic['last_write_time'].get_value(),
        'change_time':basic['change_time'].get_value(),
        'attributes':basic['file_attributes'].get_value(),
        'size':standard['end_of_file'].get_value(),
        'links':standard['number_of_links'].get_value(),
        'directory':standard['directory'].get_value(),
        'delete_pending':standard['delete_pending'].get_value(),
    }
    require(result['index']>0 and result['created']>0 and result['size']>=0
            and not result['delete_pending'] and not result['attributes'] & (0x400|0x40)
            and bool(result['attributes'] & 0x10)==result['directory'])
    if not result['directory']: require(result['links']==1)
    return result


def _identity(info): return {'index':info['index'],'created':info['created']}


class Archive:
    """One transfer per instance/connection; directory handles stay open."""
    def __init__(self,tree,value):
        self.value=dict(contract.validate(value,value.get('context_sha256'),value.get('uid')))
        self.tree=tree
        self.connection=tree.session.connection
        require(not getattr(self.connection,'_tianming_archive_claimed',False))
        self.connection._tianming_archive_claimed=True
        self.directories=[]
        self._used=False
        self._closed=False

    def _live(self):
        require(not self._closed)
        _authenticated(self.connection,self.tree.session,self.value)
        require(self.connection.handshake_done is True and self.tree._connected is True
                and self.tree.is_dfs_share is False
                and self.tree.share_name=='\\\\'+self.value['server_address']+'\\'+self.value['share']
                and self.tree.session.tree_connect_table.get(self.tree.tree_connect_id) is self.tree)

    def _open(self,name,*,directory=False,create=False,readable=False):
        from smbprotocol.open import Open
        require(type(readable) is bool and (not readable or (create and not directory)))
        self._live()
        handle=Open(self.tree,name)
        try:
            # FILE_OPEN_REPARSE_POINT prevents following the final component;
            # all fixed parents are held without FILE_SHARE_DELETE beforehand.
            handle.create(impersonation_level=2,
                desired_access=(0x80|0x20) if directory else (
                    0x80|0x2|0x10000|(0x1 if readable else 0) if create else 0x80|0x1),
                file_attributes=0x10 if directory else 0x80,
                share_access=3 if directory else (0 if create else 1),
                create_disposition=2 if create else 1,
                create_options=0x200000|(0x1 if directory else 0x40),oplock_level=0)
            require(handle.connected and handle.create_action==(2 if create else 1)
                    and type(handle.file_id) is bytes and len(handle.file_id)==16
                    and handle.file_id not in (b'\0'*16,b'\xff'*16))
            info=_query(handle)
            require(info['directory'] is directory)
            if create: require(info['size']==0)
            return handle,info
        except BaseException:
            if handle.connected:
                try: handle.close()
                except Exception: pass  # Outer open_share owns final TCP close.
            raise

    def open_directories(self):
        require(not self.directories and not self._closed)
        for name in DIRECTORIES:
            handle,info=self._open(name,directory=True)
            self.directories.append((handle,_identity(info)))
        self.check_directories()

    def check_directories(self):
        self._live()
        require(len(self.directories)==3)
        for handle,identity in self.directories:
            require(handle.connected)
            info=_query(handle)
            require(info['directory'] is True and _identity(info)==identity)

    def _begin(self):
        require(not self._used)
        self._used=True
        self.check_directories()

    def _metadata(self,kind,basename,size,sha256,identity):
        return {'type':kind,'contract_sha256':contract.digest(self.value),
                'connection_guid':self.connection.client_guid.hex,'filename':basename,
                'bytes':size,'sha256':sha256,'directories':[dict(i) for _,i in self.directories],
                'file':identity,'backup_authenticated':False}

    def _rename(self,handle,path):
        from smbprotocol.file_info import FileRenameInformation
        from smbprotocol.open import SMB2SetInfoRequest, SMB2SetInfoResponse
        rename=FileRenameInformation()
        rename['replace_if_exists']=False
        rename['root_directory']=0
        rename['file_name']=path
        request=SMB2SetInfoRequest()
        request['info_type']=rename.INFO_TYPE; request['file_info_class']=rename.INFO_CLASS
        request['file_id']=handle.file_id; request['buffer']=rename.pack()
        sent=self.connection.send(request,sid=self.tree.session.session_id,tid=self.tree.tree_connect_id)
        response=self.connection.receive(sent,resolve_symlinks=False)
        parsed=SMB2SetInfoResponse()
        require(parsed.unpack(response['data'].get_value())==b'' and parsed['structure_size'].get_value()==2)

    def upload(self,source_fd,basename,size,sha256,attempt_id):
        """Upload already authenticated original-format ciphertext; no overwrite."""
        _spec(size,sha256)
        path=contract.remote_path(self.value,basename)
        require(isinstance(attempt_id,str) and re.fullmatch('[0-9a-f]{32}',attempt_id))
        source=_local(source_fd,writable=False)
        require(source[1][0]==size and os.pread(source_fd,len(MAGIC),0)==MAGIC)
        self._begin()
        handle,initial=self._open(path+'.'+attempt_id+'.partial',create=True)
        try:
            digest=hashlib.sha256(); offset=0
            chunk=min(CHUNK,self.connection.max_write_size)
            require(type(chunk) is int and chunk>0)
            while offset<size:
                self._live()
                data=os.pread(source_fd,min(chunk,size-offset),offset)
                require(data and len(data)<=size-offset)
                written=0
                while written<len(data):
                    count=handle.write(data[written:],offset=offset+written,write_through=True)
                    require(type(count) is int and 0<count<=len(data)-written)
                    written+=count
                digest.update(data);offset+=len(data)
            require(_local(source_fd,writable=False)==source and digest.hexdigest()==sha256)
            require(handle.flush() is not None)
            complete=_query(handle)
            require(complete['directory'] is False and _identity(complete)==_identity(initial)
                    and complete['size']==size)
            self.check_directories()
            self._rename(handle,path)  # ReplaceIfExists=false; unknown outcomes retain both names.
            require(handle.flush() is not None)
            final=_query(handle)
            require(final['directory'] is False and _identity(final)==_identity(initial)
                    and final['size']==size)
            require(handle.close() is not None)
            self.check_directories()
            return self._metadata(UPLOAD,basename,size,sha256,_identity(final))
        finally:
            if handle.connected: handle.close()

    def download(self,destination_fd,upload):
        """Second connection, full ciphertext readback. Full recovery auth follows."""
        require(isinstance(upload,dict) and set(upload)=={'type','contract_sha256','connection_guid',
            'filename','bytes','sha256','directories','file','backup_authenticated'}
            and upload['type']==UPLOAD and upload['backup_authenticated'] is False
            and upload['contract_sha256']==contract.digest(self.value)
            and isinstance(upload['connection_guid'],str)
            and re.fullmatch('[0-9a-f]{32}',upload['connection_guid'])
            and upload['connection_guid']!=self.connection.client_guid.hex)
        size,sha256=upload['bytes'],upload['sha256'];_spec(size,sha256)
        path=contract.remote_path(self.value,upload['filename'])
        initial_local=_local(destination_fd,writable=True)
        require(initial_local[1][0]==0)
        self._begin()
        require(upload['directories']==[i for _,i in self.directories])
        handle,initial=self._open(path)
        try:
            require(initial['size']==size and _identity(initial)==upload['file'])
            digest=hashlib.sha256();offset=0
            chunk=min(CHUNK,self.connection.max_read_size)
            require(type(chunk) is int and chunk>0)
            while offset<size:
                self._live()
                data=handle.read(offset,min(chunk,size-offset))
                require(type(data) is bytes and 0<len(data)<=min(chunk,size-offset))
                written=0
                while written<len(data):
                    count=os.pwrite(destination_fd,data[written:],offset+written)
                    require(count>0);written+=count
                digest.update(data);offset+=len(data)
            final=_query(handle)
            require(final==initial and digest.hexdigest()==sha256)
            require(handle.close() is not None)
            self.check_directories()
            os.fsync(destination_fd)
            local=_local(destination_fd,writable=True)
            require(local[0]==initial_local[0] and local[1][0]==size
                    and os.pread(destination_fd,len(MAGIC),0)==MAGIC)
            observed=hashlib.sha256()
            for offset in range(0,size,CHUNK):
                data=os.pread(destination_fd,min(CHUNK,size-offset),offset)
                require(len(data)==min(CHUNK,size-offset));observed.update(data)
            require(observed.hexdigest()==sha256 and _local(destination_fd,writable=True)==local)
            return self._metadata(READBACK,upload['filename'],size,sha256,_identity(final))
        finally:
            if handle.connected: handle.close()

    def close(self):
        errors=[]
        for handle,_ in reversed(self.directories):
            if handle.connected:
                try: require(handle.close() is not None)
                except Exception as error: errors.append(type(error).__name__)
        self._closed=True
        require(not errors)


@contextmanager
def open_archive(tree,value):
    archive=Archive(tree,value)
    try:
        archive.open_directories()
        yield archive
        archive.check_directories()
    except Exception:
        raise ValueError(ERROR) from None
    finally: archive.close()
