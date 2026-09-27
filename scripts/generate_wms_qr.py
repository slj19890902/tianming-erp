from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import qrcode

from app.services.mobile_qr import incoming_mobile_url


def build_wms_url() -> str:
    """Use the same configured mobile origin as every server-generated label."""

    return incoming_mobile_url()


def generate_qr(url: str, output: Path) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    image = qrcode.make(url)
    image.save(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="生成车间 WMS 固定入口二维码")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "wms_entry_qr.png",
    )
    args = parser.parse_args()
    url = build_wms_url()
    output = generate_qr(url, args.output)
    print(f"WMS 地址: {url}")
    print(f"二维码文件: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
