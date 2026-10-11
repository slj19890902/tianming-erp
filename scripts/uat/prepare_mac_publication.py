"""Create a reviewable publication plan from a factory-signed native package."""
import argparse
import json
import os
from pathlib import Path

from desktop_assistant.mac_release_publish import prepare
from desktop_assistant.mac_keychain_install import canonical, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--publisher-sha256', required=True)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--installer-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pristine-bootstrap', action='store_true',
                        help='首次发布到已配置且完全为空的固定实例；不恢复数据或启动服务')
    args = parser.parse_args()
    value = prepare(args.package,args.public_key.read_bytes(),args.context.read_bytes(),
                    args.installer_manifest.read_bytes(),publisher_sha256=args.publisher_sha256,
                    pristine_bootstrap=args.pristine_bootstrap)
    raw = canonical(value)
    fd = os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        os.fchmod(stream.fileno(),0o600)
        stream.write(raw);stream.flush();os.fsync(stream.fileno())
    if args.output.read_bytes() != raw: raise ValueError('Publication plan readback failed')
    print(json.dumps({'plan_sha256':digest(raw),'package_sha256':value['package_sha256'],'plan_type':value['type'],
                      'system_modified':False,'service_started':False,'factory_activated':False}))


if __name__ == '__main__': main()
