"""Dedicated management entry. No passwords, arbitrary paths or commands."""
import os
from pathlib import Path
import sys


def main():
    if (sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.getuid() == 0 or os.geteuid() == 0 or len(sys.argv) != 1):
        raise ValueError('专用管理需要隔离服务身份')
    import pwd
    if any(pwd.getpwuid(uid).pw_name != '_tianmingerp' for uid in {os.getuid(), os.geteuid()}):
        raise ValueError('专用管理身份不符')
    os.umask(0o077)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.mac_service_manage import main as manage
    manage(script=__file__, executable=sys.executable)


if __name__ == '__main__':
    try: main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit('专用管理未确认完成；可能已变更状态，保留现场并重新查看状态。不输出凭据或异常详情。') from None
