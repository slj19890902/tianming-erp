"""Pre-stop NAS write/read/rename proof over one authenticated stream.

Only an exclusively created 16 KiB random canary can be removed, through that
same held handle and only after content/identity/namespace checks. No existing
file deletion, filename cleanup after failure, retry, or backup success claim.
"""
import hashlib
import os
import re

from . import mac_smb_stream_files as files, mac_smb_stream_contract as contract

TYPE='tianming.mac-smb-stream-preflight.v1'
SIZE=16384
FIELDS={'type','contract_sha256','connection_guid','attempt_id','directories','file',
        'bytes','sha256','available_bytes','required_bytes','canary_removed','backup_authenticated'}


def require(ok):
    if not ok: raise ValueError('NAS流读写预检未确认；未允许停止ERP，保留未知测试现场')


def specification(attempt,required):
    require(isinstance(attempt,str) and re.fullmatch('[0-9a-f]{32}',attempt)
            and type(required) is int and SIZE<=required<2**63)


def validate(proof,value,attempt,required):
    specification(attempt,required)
    require(isinstance(proof,dict) and set(proof)==FIELDS and proof['type']==TYPE
        and proof['contract_sha256']==contract.digest(value) and proof['attempt_id']==attempt
        and type(proof['bytes']) is int and proof['bytes']==SIZE
        and proof['canary_removed'] is True and proof['backup_authenticated'] is False
        and type(proof['required_bytes']) is int and proof['required_bytes']==required
        and type(proof['available_bytes']) is int and required<=proof['available_bytes']<2**63
        and isinstance(proof['connection_guid'],str) and re.fullmatch('[0-9a-f]{32}',proof['connection_guid'])
        and isinstance(proof['sha256'],str) and re.fullmatch('[0-9a-f]{64}',proof['sha256'])
        and isinstance(proof['directories'],list) and len(proof['directories'])==3)
    for item in [*proof['directories'],proof['file']]:
        require(isinstance(item,dict) and set(item)=={'index','created'}
                and all(type(v) is int and 0<v<2**64 for v in item.values()))
    return proof


class Probe(files.Archive):
    def space(self):
        # MS-FSCC 2.5.4: total is caller-visible (possibly quota-limited), while
        # actual_available is for the volume and can exceed that caller total.
        from smbprotocol.file_info import FileFsFullSizeInformation
        from smbprotocol.open import SMB2QueryInfoRequest, SMB2QueryInfoResponse
        self.check_directories()
        request=SMB2QueryInfoRequest();request['file_id']=self.directories[-1][0].file_id
        request['info_type']=FileFsFullSizeInformation.INFO_TYPE
        request['file_info_class']=FileFsFullSizeInformation.INFO_CLASS
        request['output_buffer_length']=32
        sent=self.connection.send(request,sid=self.tree.session.session_id,tid=self.tree.tree_connect_id)
        response=self.connection.receive(sent,resolve_symlinks=False)
        parsed=SMB2QueryInfoResponse();require(parsed.unpack(response['data'].get_value())==b'')
        raw=parsed['buffer'].get_value();require(len(raw)==32)
        info=FileFsFullSizeInformation();require(info.unpack(raw)==b'')
        total,caller,actual,sectors,sector=(info[k].get_value() for k in
            ('total_allocation_units','caller_available_units','actual_available_units','sectors_per_unit','bytes_per_sector'))
        require(all(type(v) is int for v in (total,caller,actual,sectors,sector))
            and 0<=caller<=total<2**63 and 0<=actual<2**63 and 0<sectors<2**32 and 0<sector<2**32)
        available=min(caller,actual)*sectors*sector
        require(0<=available<2**63)
        return available

    def absent(self,path):
        from smbprotocol.exceptions import ObjectNameNotFound
        try: handle,_=self._open(path)
        except ObjectNameNotFound: return
        try: require(False)
        finally:
            if handle.connected: handle.close()

    def remove_created(self,handle):
        # MS-FSCC 2.4.11: mark only our held FILE_CREATE object for deletion.
        # A successful SET_INFO is not absence proof; CLOSE and lookup follow.
        from smbprotocol.file_info import FileDispositionInformation
        from smbprotocol.open import SMB2SetInfoRequest, SMB2SetInfoResponse
        info=FileDispositionInformation();info['delete_pending']=True
        request=SMB2SetInfoRequest();request['file_id']=handle.file_id
        request['info_type']=info.INFO_TYPE;request['file_info_class']=info.INFO_CLASS
        request['buffer']=info.pack()
        sent=self.connection.send(request,sid=self.tree.session.session_id,tid=self.tree.tree_connect_id)
        response=self.connection.receive(sent,resolve_symlinks=False)
        parsed=SMB2SetInfoResponse()
        require(parsed.unpack(response['data'].get_value())==b'' and parsed['structure_size'].get_value()==2)
        require(handle.close() is not None)

    def preflight(self,attempt,required):
        specification(attempt,required);self._begin()
        require(self.space()>=required)
        prefix=contract.DIRECTORY.replace('/','\\')+'\\tianming-connectivity-'+attempt
        pending,final=prefix+'.partial',prefix+'.verified'
        payload=os.urandom(SIZE)
        handle,initial=self._open(pending,create=True,readable=True)
        try:
            offset=0;chunk=min(files.CHUNK,self.connection.max_write_size)
            require(type(chunk) is int and chunk>0)
            while offset<SIZE:
                self._live()
                data=payload[offset:offset+chunk]
                count=handle.write(data,offset=offset,write_through=True)
                require(type(count) is int and 0<count<=len(data));offset+=count
            require(handle.flush() is not None)
            complete=files._query(handle)
            require(files._identity(complete)==files._identity(initial) and complete['size']==SIZE)
            observed=bytearray();chunk=min(files.CHUNK,self.connection.max_read_size)
            require(type(chunk) is int and chunk>0)
            while len(observed)<SIZE:
                self._live();size=min(chunk,SIZE-len(observed))
                data=handle.read(len(observed),size)
                require(type(data) is bytes and 0<len(data)<=size);observed.extend(data)
            require(bytes(observed)==payload and files._query(handle)==complete)
            self.check_directories();self._rename(handle,final)
            require(handle.flush() is not None)
            renamed=files._query(handle)
            require(files._identity(renamed)==files._identity(initial) and renamed['size']==SIZE)
            self.absent(pending);self.check_directories()
            # No cleanup request is made on any earlier failed path.
            self.remove_created(handle)
            self.absent(final);self.absent(pending);self.check_directories()
            available=self.space();require(available>=required)
            proof=dict(type=TYPE,contract_sha256=contract.digest(self.value),
                connection_guid=self.connection.client_guid.hex,attempt_id=attempt,
                directories=[dict(identity) for _,identity in self.directories],file=files._identity(initial),
                bytes=SIZE,sha256=hashlib.sha256(payload).hexdigest(),available_bytes=available,
                required_bytes=required,canary_removed=True,backup_authenticated=False)
            return validate(proof,self.value,attempt,required)
        finally:
            if handle.connected: handle.close()


def check(tree,value,attempt,required):
    probe=Probe(tree,value)
    try:
        probe.open_directories()
        result=probe.preflight(attempt,required)
        probe.check_directories()
        return result
    finally: probe.close()
