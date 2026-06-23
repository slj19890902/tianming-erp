from __future__ import annotations

import argparse
import ipaddress
import socket
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import qrcode

from app.core.config import load_settings


def select_lan_ip(candidates: list[str]) -> str:
    ranked: list[tuple[int, str]] = []
    for candidate in set(candidates):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.version != 4
            or address.is_loopback
            or address.is_link_local
            or not address.is_private
            or address in ipaddress.ip_network("198.18.0.0/15")
        ):
            continue
        if candidate.startswith("192.168."):
            priority = 0
        elif candidate.startswith("10."):
            priority = 1
        else:
            priority = 2
        ranked.append((priority, candidate))
    if not ranked:
        return "127.0.0.1"
    return min(ranked)[1]


def discover_lan_ip() -> str:
    candidates = list(socket.gethostbyname_ex(socket.gethostname())[2])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        try:
            connection.connect(("8.8.8.8", 80))
            candidates.append(str(connection.getsockname()[0]))
        except OSError:
            pass
    return select_lan_ip(candidates)


def build_wms_url(ip_address: str, port: int) -> str:
    address = ip_address.strip()
    if not address:
        raise ValueError("局域网 IP 不能为空")
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    return f"http://{address}:{port}/incoming.html"


def generate_qr(url: str, output: Path) -> Path:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    image = qrcode.make(url)
    image.save(output)
    return output


def main() -> int:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="生成车间 WMS 固定入口二维码")
    parser.add_argument(
        "--ip",
        default=None,
        help="本机局域网 IPv4；未提供时自动探测",
    )
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "wms_entry_qr.png",
    )
    args = parser.parse_args()
    url = build_wms_url(args.ip or discover_lan_ip(), args.port)
    output = generate_qr(url, args.output)
    print(f"WMS 地址: {url}")
    print(f"二维码文件: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
