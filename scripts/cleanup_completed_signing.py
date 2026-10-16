"""Explicit developer-side cleanup after task acceptance; never invoked by a build."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_assistant.build import remove_identical_signing_duplicates


def main() -> int:
    parser = argparse.ArgumentParser(
        description='回收已验收构建的同字节签名暂存副本',
        epilog=('凭据 JSON 需含 type=tianming.build-completion.v1、status=accepted、'
                'output=规范化构建路径、package_sha256=正式原包摘要、dependencies_checked=true。'
                '仅在人工验收及后续引用核查完成后生成。'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--completion-receipt', required=True, type=Path)
    parser.add_argument('--published-package', required=True, type=Path)
    args = parser.parse_args()
    result = remove_identical_signing_duplicates(
        args.output, completion_receipt=args.completion_receipt,
        published_package=args.published_package)
    print(json.dumps(result, ensure_ascii=False))
    return 2 if result['retained'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
