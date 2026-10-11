"""Dedicated service restore entry. Launch only with the approved Python -I -B."""
import os
from pathlib import Path
import sys


def main():
    # Reject root/ordinary users before importing the ERP or native credential API.
    if (sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.getuid() == 0 or os.geteuid() == 0):
        raise ValueError('专用恢复必须以服务身份和隔离解释器运行')
    import pwd
    if any(pwd.getpwuid(uid).pw_name != '_tianmingerp' for uid in {os.getuid(), os.geteuid()}):
        raise ValueError('专用恢复身份不符')
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.mac_service_restore import main as restore
    restore(script=__file__, executable=sys.executable)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit('专用恢复未确认完成；未启动ERP。保留输入与恢复现场，勿覆盖重试；不输出秘密或异常详情。') from None
