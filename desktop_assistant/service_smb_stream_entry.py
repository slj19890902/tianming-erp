"""Pinned, isolated stream worker; no argv credentials or caller code paths."""
import os
from pathlib import Path
import sys


def main():
    if (sys.platform!='darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.geteuid()==0 or len(sys.argv)!=1):
        raise ValueError('SMB流传输要求隔离专用服务身份')
    import pwd
    if any(pwd.getpwuid(uid).pw_name!='_tianmingerp' for uid in {os.getuid(),os.geteuid()}):
        raise ValueError('SMB流传输身份不符')
    os.umask(0o077)
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.mac_smb_stream_runtime import main as transfer
    transfer(script=__file__,executable=sys.executable)


if __name__=='__main__':
    try: main()
    except (Exception,KeyboardInterrupt):
        raise SystemExit('SMB流传输未确认；保留现场，不覆盖重试，不输出秘密。') from None
