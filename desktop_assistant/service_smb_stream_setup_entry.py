"""Dedicated signed NAS provisioning entry; passwords are never arguments."""
import os
from pathlib import Path
import sys


def main():
    if (sys.platform!='darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.geteuid()==0):
        raise ValueError('NAS配置要求隔离专用服务身份')
    import pwd
    if any(pwd.getpwuid(uid).pw_name!='_tianmingerp' for uid in {os.getuid(),os.geteuid()}):
        raise ValueError('NAS配置身份不符')
    os.umask(0o077)
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    from desktop_assistant.mac_smb_stream_setup import main as setup
    setup(script=__file__,executable=sys.executable)


if __name__=='__main__':
    try: main()
    except (Exception,KeyboardInterrupt):
        raise SystemExit('NAS流配置未确认；保留现场，不覆盖重试，不输出密码或异常详情。') from None
