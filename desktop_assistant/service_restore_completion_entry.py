"""Signed, dedicated-UID-only entry for a promoted restore checkpoint."""
import os
from pathlib import Path
import sys


def main():
    if (sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.getuid() == 0 or os.geteuid() == 0):
        raise ValueError('有限恢复完成必须使用专用身份及隔离解释器')
    import pwd
    if any(pwd.getpwuid(uid).pw_name != '_tianmingerp' for uid in {os.getuid(), os.geteuid()}):
        raise ValueError('有限恢复完成身份不符')
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.mac_restore_completion import main as complete
    complete(script=__file__, executable=sys.executable)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit('有限恢复完成未确认；保留现场，不覆盖重试、不输出秘密或异常详情。') from None
