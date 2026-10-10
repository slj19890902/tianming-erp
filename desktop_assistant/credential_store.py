"""Host credential routing; publisher identity remains Windows-only."""
from functools import partial
import sys

from app.core.mac_keychain import PURPOSES


def protect(value, purpose):
    if purpose not in PURPOSES:
        raise ValueError('不支持的 ERP 凭据用途')
    if sys.platform == 'darwin':
        from app.core.mac_keychain import protect as store
        return store(value, purpose)
    if sys.platform == 'win32':
        from desktop_assistant.windows import protect as store
        return store(value)
    raise ValueError('当前平台不支持安全凭据存储')


def unprotect(value, purpose):
    if purpose not in PURPOSES:
        raise ValueError('不支持的 ERP 凭据用途')
    if sys.platform == 'darwin':
        from app.core.mac_keychain import unprotect as load
        return load(value, purpose)
    if sys.platform == 'win32':
        if value.startswith('tm-keychain-'):
            raise ValueError('Mac 钥匙串引用需要安全移交，不能由 Windows DPAPI 读取')
        from desktop_assistant.windows import unprotect as load
        return load(value)
    raise ValueError('当前平台不支持安全凭据存储')


protect_backup = partial(protect, purpose='backup')
unprotect_backup = partial(unprotect, purpose='backup')
