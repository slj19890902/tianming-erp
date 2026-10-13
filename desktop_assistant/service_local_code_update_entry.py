"""Dedicated same-schema LOCAL checkpoint updater; no NAS success claims."""
import os
import sys
from pathlib import Path


def main():
    if (sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.getuid() == 0 or os.geteuid() == 0 or len(sys.argv) != 1):
        raise ValueError('本地更新需要隔离专用服务身份')
    import pwd
    if any(pwd.getpwuid(uid).pw_name != '_tianmingerp' for uid in {os.getuid(), os.geteuid()}):
        raise ValueError('专用服务身份无效')
    os.umask(0o077)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.local_code_update import main as update
    update(script=__file__, executable=sys.executable)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit('本地程序更新未确认；保留完整业务、备份和现场，不覆盖重试。') from None
