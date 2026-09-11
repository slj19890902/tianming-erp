"""Publish an already built, signed package to NAS; never publishes raw Git checkout."""
import argparse
from pathlib import Path
import shutil
import tempfile

from desktop_assistant.storage import extract_verified, sha, write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--nas-releases', type=Path, required=True)
    args = parser.parse_args()
    if not args.nas_releases.is_dir():
        raise ValueError('必须指定已存在的NAS发布目录')
    with tempfile.TemporaryDirectory(prefix='tm-release-verify-') as temp:
        manifest = extract_verified(args.package, Path(temp) / 'verified', args.public_key.read_bytes())
    identity = sha(args.package)
    name = identity + '.zip'
    final = args.nas_releases / name
    if not final.exists():
        pending = args.nas_releases / (name + '.pending')
        with args.package.open('rb') as source, pending.open('xb') as target:
            shutil.copyfileobj(source, target, 1024*1024)
        if sha(pending) != identity:
            raise ValueError('NAS发布包校验失败')
        pending.rename(final)
    if sha(final) != identity:
        raise ValueError('NAS已有同名文件校验失败')
    write_json(args.nas_releases / 'latest.json', {'package': name, 'version': manifest['version'],
               'git_sha': manifest['git_sha']})
    print(manifest['version'], identity)


if __name__ == '__main__':
    main()
