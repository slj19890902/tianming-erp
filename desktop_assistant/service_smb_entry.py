"""Fixed service SMB entry; invoke through the approved NAS-only launcher."""
import os
from pathlib import Path
import sys


def main():
    if (sys.platform != 'darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.getuid() == 0 or os.geteuid() == 0 or len(sys.argv) != 1):
        raise ValueError('SMB连接要求隔离专用服务身份')
    import pwd
    if any(pwd.getpwuid(uid).pw_name != '_tianmingerp' for uid in {os.getuid(),os.geteuid()}):
        raise ValueError('SMB专用身份不符')
    os.umask(0o077)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.mac_smb_runtime import main as connect
    connect(script=__file__, executable=sys.executable)


if __name__ == '__main__':
    try: main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit('专用SMB后台连接未确认；保留挂载和记录，不自动覆盖重试，不输出秘密。') from None
