from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from io import BytesIO

from reportlab.lib.utils import ImageReader


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CONTRACT_SEAL_BYTES = 2 * 1024 * 1024
MAX_CONTRACT_SEAL_DIMENSION = 4096


class ContractSealImageError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedContractSeal:
    content: bytes
    sha256: str
    width_px: int
    height_px: int


def validate_contract_seal_png(content: bytes) -> ValidatedContractSeal:
    if not content:
        raise ContractSealImageError("印章 PNG 文件为空")
    if len(content) > MAX_CONTRACT_SEAL_BYTES:
        raise ContractSealImageError("印章 PNG 不能超过 2 MB")
    if len(content) < 24 or not content.startswith(PNG_SIGNATURE):
        raise ContractSealImageError("只允许上传 PNG 格式的公司印章")
    width_px, height_px = struct.unpack(">II", content[16:24])
    if (
        width_px <= 0
        or height_px <= 0
        or width_px > MAX_CONTRACT_SEAL_DIMENSION
        or height_px > MAX_CONTRACT_SEAL_DIMENSION
    ):
        raise ContractSealImageError("印章 PNG 尺寸无效或超过 4096 像素")
    try:
        image = ImageReader(BytesIO(content))
        decoded_width, decoded_height = image.getSize()
    except Exception as exc:
        raise ContractSealImageError("印章 PNG 无法解析或文件已损坏") from exc
    if (decoded_width, decoded_height) != (width_px, height_px):
        raise ContractSealImageError("印章 PNG 尺寸信息不一致")
    return ValidatedContractSeal(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        width_px=width_px,
        height_px=height_px,
    )
