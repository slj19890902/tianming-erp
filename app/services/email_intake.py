"""Bounded read-only IMAP adapter for the owner's confirmed 126 inbox."""
import base64
import ctypes
import hashlib
import imaplib
import os
import re
import ssl
import threading
import asyncio
import logging
import json
from app.core.home_rehearsal import require_external_access
from app.services.email_sender_filter import normalize_senders, sender_matches
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath
from sqlalchemy import select
from app.models.email_intake import EmailIntakeSettings, EmailIntakeMessage, EmailIntakeAttachment
from app.services.audit_log import append_audit_event

ACCOUNT = 'sz_tmbz@126.com'
MAILBOX_KEY = ACCOUNT + '/INBOX'
MAX_MESSAGE = 16 * 1024 * 1024
MAX_ATTACHMENT = 8 * 1024 * 1024
sync_lock = threading.Lock()
log = logging.getLogger(__name__)


def protect(value, decrypt=False):
    """Windows DPAPI user-bound secret; never store an adjacent plaintext key."""
    if os.name != 'nt':
        raise ValueError('邮箱授权码安全保存当前需要Windows服务器')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_char))]
    raw = base64.b64decode(value, validate=True) if decrypt else value.encode()
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError('邮箱授权码无法由当前Windows账号读取，请重新保存授权码')
    try:
        result = ctypes.string_at(target.data, target.size)
        return result.decode() if decrypt else base64.b64encode(result).decode()
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.data)


def secret(db):
    settings = db.get(EmailIntakeSettings, 1)
    if not settings:
        raise ValueError('请先在邮箱设置中保存126客户端授权码')
    return protect(settings.encrypted_secret, decrypt=True)


def audit(db, user, action, details):
    append_audit_event(db, event_category='system', result='success',
                      source='web' if user is not None else 'system',
                      module_code='email_intake', action_code=action, resource='email_intake',
                      actor=user, operator_name=None if user is not None else '系统自动收件',
                      details=details)


def parse_message(raw):
    if len(raw) > MAX_MESSAGE:
        raise ValueError('邮件超过16MB，请在邮箱中查看')
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    result = {key: str(mail.get(header, ''))[:limit] for key, header, limit in (
        ('subject', 'Subject', 1000), ('sender', 'From', 1000), ('received', 'Date', 100),
        ('message_id', 'Message-ID', 1000))}
    bodies, attachments, notices = [], [], []
    for number, part in enumerate(mail.walk()):
        if number > 200:
            notices.append('邮件段落超过限制，其余内容请在邮箱查看'); break
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name:
            filename = PurePosixPath(str(name).replace('\\', '/')).name
            filename = re.sub(r'[\x00-\x1f\x7f]', '', filename)[:240] or 'attachment'
            if PurePosixPath(filename).suffix.lower() not in {'.pdf', '.xlsx', '.xls'}:
                notices.append('未收录非PDF/Excel附件：' + filename); continue
            data = part.get_payload(decode=True) or b''
            if not data or len(data) > MAX_ATTACHMENT or len(attachments) >= 20:
                notices.append('附件为空或超过单附件8MB/20个附件限制：' + filename); continue
            # Files remain inert bytes. Preview validation is performed by the existing parser.
            attachments.append({'part_number': number, 'filename': filename, 'content': data,
                                'sha256': hashlib.sha256(data).hexdigest()})
        elif part.get_content_type() == 'text/plain':
            data = part.get_payload(decode=True) or b''
            try:
                bodies.append(data.decode(part.get_content_charset() or 'utf-8', errors='replace')[:10000])
            except LookupError:
                bodies.append(data.decode('utf-8', errors='replace')[:10000])
    result['body'] = '\n'.join(bodies)[:20000]
    if not result['body']:
        notices.append('无纯文本正文；HTML及远程图片不加载，请结合附件或原邮箱查看')
    result['notice'] = '；'.join(notices)[:5000]
    return result, attachments


def store_message(db, user, validity, uid, raw=None, notice=''):
    existing = db.scalar(select(EmailIntakeMessage).where(
        EmailIntakeMessage.mailbox_key == MAILBOX_KEY,
        EmailIntakeMessage.uid_validity == validity, EmailIntakeMessage.uid == uid))
    if existing:
        return False
    values, attachments = parse_message(raw) if raw is not None else ({'notice': notice}, [])
    row = EmailIntakeMessage(mailbox_key=MAILBOX_KEY, uid_validity=validity, uid=uid,
                             status='pending' if raw is not None else 'oversize', **values)
    db.add(row); db.flush()
    for attachment in attachments:
        previous = db.scalar(select(EmailIntakeAttachment.id).where(
            EmailIntakeAttachment.sha256 == attachment['sha256']).order_by(EmailIntakeAttachment.id).limit(1))
        db.add(EmailIntakeAttachment(message_id=row.id, duplicate_of=previous, **attachment))
        db.flush()
    audit(db, user, 'receive', {'message_id': row.id, 'attachment_count': len(attachments)})
    db.commit()
    return True


def sync_inbox(db, user, factory=imaplib.IMAP4_SSL):
    require_external_access()
    if not sync_lock.acquire(blocking=False):
        raise LookupError('正在读取邮箱，请稍后刷新列表')
    client = None
    try:
        password = secret(db)
        settings = db.get(EmailIntakeSettings, 1)
        addresses = (normalize_senders(json.loads(settings.sender_addresses_json))
                     if settings.sender_addresses_json is not None else None)
        if addresses == []:
            return {'received': 0, 'remaining': 0, 'notice': '发件人名单为空，请先添加需要收单的邮箱地址'}
        client = factory('imap.126.com', 993, ssl_context=ssl.create_default_context(), timeout=20)
        client.login(ACCOUNT, password)
        # NetEase may require the standard client ID before EXAMINE.
        if b'ID' in client.capabilities or 'ID' in client.capabilities:
            imaplib.Commands.setdefault('ID', ('AUTH', 'SELECTED'))
            client._simple_command('ID', '("name" "TianmingERP" "version" "1.0" "contact" "sz_tmbz@126.com")')
        status, _ = client.select('INBOX', readonly=True)
        if status != 'OK':
            raise ValueError('收件箱读取被拒绝，请检查126 IMAP服务与授权码')
        _, values = client.response('UIDVALIDITY')
        validity = (values[0] or b'').decode() if values else ''
        if not validity.isdigit():
            raise ValueError('邮箱未返回稳定标识，本次未导入')
        candidate_uids = set()
        for address in addresses or [None]:
            criteria = ('FROM', '"' + address + '"') if address else ('ALL',)
            from app.services.email_pdf_queue import INBOX_SINCE
            status, values = client.uid('search', None, *criteria, 'SINCE', INBOX_SINCE)
            if status != 'OK':
                raise ValueError('邮件列表读取失败，请重试')
            candidate_uids.update(int(uid) for uid in (values[0] or b'').split())
        known = set(db.scalars(select(EmailIntakeMessage.uid).where(
            EmailIntakeMessage.mailbox_key == MAILBOX_KEY, EmailIntakeMessage.uid_validity == validity)))
        remaining = sorted(candidate_uids - known)
        if addresses is not None:
            # IMAP FROM is a substring search. Fetch headers only to enforce exact
            # mailbox equality before downloading any body or attachment.
            exact = []
            for offset in range(0, len(remaining), 100):
                uid_set = ','.join(str(uid) for uid in remaining[offset:offset + 100])
                status, headers = client.uid('fetch', uid_set, '(UID BODY.PEEK[HEADER.FIELDS (FROM)])')
                if status != 'OK':
                    raise ValueError('发件人校验失败，本次未下载邮件正文')
                checked = set()
                for entry in headers:
                    if not isinstance(entry, tuple):
                        continue
                    match = re.search(rb'\bUID\s+(\d+)', entry[0])
                    if not match:
                        raise ValueError('发件人校验缺少邮件标识，请重试')
                    checked.add(int(match[1]))
                    header = BytesParser(policy=policy.default).parsebytes(entry[1])
                    if sender_matches([str(value) for value in header.get_all('From', [])], addresses):
                        exact.append(int(match[1]))
                if checked != set(remaining[offset:offset + 100]):
                    raise ValueError('发件人校验返回不完整，请重试')
            remaining = sorted(set(exact) & set(remaining))
        count = 0
        for uid in remaining[:20]:
            status, metadata = client.uid('fetch', str(uid), '(RFC822.SIZE)')
            size = re.search(rb'RFC822.SIZE\s+(\d+)', b' '.join(v for v in metadata if isinstance(v, bytes)))
            if status != 'OK' or not size:
                raise ValueError('邮件大小读取失败，已完成项保留，请重试')
            if int(size[1]) > MAX_MESSAGE:
                count += store_message(db, user, validity, uid, notice='邮件超过16MB，请从原邮箱下载处理')
                continue
            status, content = client.uid('fetch', str(uid), '(BODY.PEEK[])')
            raw = next((v[1] for v in content if isinstance(v, tuple)), None)
            if status != 'OK' or raw is None:
                raise ValueError('邮件正文读取失败，已完成项保留，请重试')
            if addresses is not None:
                header = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
                if not sender_matches([str(value) for value in header.get_all('From', [])], addresses):
                    raise ValueError('邮件发件人与筛选结果不一致，本封未导入')
            count += store_message(db, user, validity, uid, raw)
        return {'received': count, 'remaining': max(0, len(remaining) - 20)}
    finally:
        if client:
            try:
                client.logout()
            except (OSError, imaplib.IMAP4.error):
                pass
        sync_lock.release()


def _safe_automatic_error(error):
    if isinstance(error, LookupError):
        return '已有收件任务正在运行，下次自动重试'
    if isinstance(error, (OSError, imaplib.IMAP4.error)):
        return '126邮箱连接失败，下次自动重试'
    if isinstance(error, ValueError):
        text = str(error).strip()
        return text[:500] if text else '邮箱配置或邮件内容校验失败'
    return '自动读取失败，下次自动重试'


def run_automatic_cycle(session_factory, factory=imaplib.IMAP4_SSL):
    """Run one configured inbox read without creating sales facts."""
    require_external_access()
    with session_factory() as db:
        settings = db.get(EmailIntakeSettings, 1)
        if settings is None or not settings.automatic_enabled:
            return {'enabled': False, 'received': 0, 'remaining': 0}
        settings.last_sync_started_at = datetime.now()
        settings.last_sync_status = 'running'
        settings.last_sync_error = None
        db.commit()
        try:
            result = sync_inbox(db, None, factory)
        except Exception as error:
            db.rollback()
            settings = db.get(EmailIntakeSettings, 1)
            if settings is not None:
                settings.last_sync_completed_at = datetime.now()
                settings.last_sync_status = 'failed'
                settings.last_sync_error = _safe_automatic_error(error)
                db.commit()
            raise
        settings = db.get(EmailIntakeSettings, 1)
        if settings is not None:
            settings.last_sync_completed_at = datetime.now()
            settings.last_sync_status = 'success'
            settings.last_sync_received = int(result['received'])
            settings.last_sync_remaining = int(result['remaining'])
            settings.last_sync_error = None
            db.commit()
        return {'enabled': True, **result}


async def automatic_sync_loop(stop, session_factory, default_interval_seconds=300):
    """Poll the configured inbox in the single formal worker."""
    delay = 1
    while not stop.is_set():
        try:
            result = await asyncio.to_thread(run_automatic_cycle, session_factory)
            with session_factory() as db:
                settings = db.get(EmailIntakeSettings, 1)
                configured_minutes = int(settings.sync_interval_minutes) if settings else 5
            if result.get('enabled') and int(result.get('remaining') or 0) > 0:
                delay = 5
            else:
                delay = max(60, min(3600, configured_minutes * 60)) if settings else default_interval_seconds
        except Exception:
            log.exception('邮箱自动读取失败；已完成邮件保留，下次重试')
            delay = max(60, default_interval_seconds)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
