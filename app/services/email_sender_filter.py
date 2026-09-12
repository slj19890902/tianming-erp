"""Validate an explicit sender allowlist and match complete mailbox addresses."""
import re
from email.utils import getaddresses

_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}\Z")


def normalize_senders(values: list[str]) -> list[str]:
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError('最多配置100个发件人邮箱')
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError('请输入完整的发件人邮箱地址')
        address = value.strip().lower()
        if len(address) > 254 or not _ADDRESS.fullmatch(address):
            raise ValueError('请输入完整的发件人邮箱地址')
        if address not in result:
            result.append(address)
    return result


def sender_matches(from_headers: list[str], allowed: list[str]) -> bool:
    # Multiple From mailboxes are ambiguous: do not accept a mixed sender header.
    addresses = getaddresses(from_headers)
    return len(addresses) == 1 and addresses[0][1].lower() in set(allowed)
