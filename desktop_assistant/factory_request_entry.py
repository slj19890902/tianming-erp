"""Read-only target request entry; no command line credentials or options."""
import os
from pathlib import Path
import sys


def main():
    if (sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.getuid() == 0 or os.geteuid() == 0 or len(sys.argv) != 1):
        raise ValueError('目标申请需要隔离服务身份')
    import pwd
    if any(pwd.getpwuid(uid).pw_name != '_tianmingerp' for uid in {os.getuid(),os.geteuid()}):
        raise ValueError('目标申请身份不符')
    os.umask(0o077)
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.mac_factory_request import main as request
    request(script=__file__, executable=sys.executable)


if __name__ == '__main__':
    try: main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit('目标切换申请未确认；保持家庭隔离，未自动停启或激活。') from None
