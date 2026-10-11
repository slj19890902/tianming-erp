"""Thin dedicated-UID archive probe entry; rejects root before ERP imports."""
import os
from pathlib import Path
import sys


def main():
    if (sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.getuid() == 0 or os.geteuid() == 0 or len(sys.argv) != 1):
        raise ValueError('归档探针必须由专用隔离解释器运行')
    import pwd
    if any(pwd.getpwuid(uid).pw_name != '_tianmingerp' for uid in {os.getuid(), os.geteuid()}):
        raise ValueError('归档探针专用身份不符')
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.mac_archive_probe import main as probe
    probe(script=__file__, executable=sys.executable)


if __name__ == '__main__':
    try: main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit('归档安装验证未完成；保留配置及探针现场，不覆盖重试。未启动ERP。') from None
