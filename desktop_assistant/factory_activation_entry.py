"""Dedicated activation proof entry; credentials never travel over its IPC."""
import os
from pathlib import Path
import sys


def main():
    if (sys.platform!='darwin' or not sys.flags.isolated or not sys.dont_write_bytecode
            or os.getuid()==0 or os.geteuid()==0 or len(sys.argv)!=1):
        raise ValueError('激活核验需要隔离服务身份')
    import pwd
    if any(pwd.getpwuid(uid).pw_name!='_tianmingerp' for uid in {os.getuid(),os.geteuid()}):
        raise ValueError('激活核验身份不符')
    os.umask(0o077)
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
    os.environ['ERP_HOME_REHEARSAL']='1'
    from app.core.home_rehearsal import install_network_guard
    install_network_guard()
    from desktop_assistant.mac_factory_activation import main as verify
    verify(script=__file__,executable=sys.executable)


if __name__=='__main__':
    try: main()
    except (Exception,KeyboardInterrupt):
        raise SystemExit('最终激活核验未确认；保持停止并保留全部现场。') from None
