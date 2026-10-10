from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.product_photo_batch import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_MAX_EDGE,
    DEFAULT_MAX_SAMPLES_PER_VOLUME,
    ProductPhotoBatchError,
    build_product_photo_workbooks,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return parsed


def _write_result_payload(payload: dict[str, object], path: Path | None) -> None:
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if path is None:
        print(serialized)
        return
    path.write_text(serialized, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "批量读取照片文件夹，转正压缩后嵌入常用箱 Excel，"
            "并按 ERP 20MiB 上限自动拆卷。不会写 ERP 或数据库。"
        )
    )
    parser.add_argument("--template", required=True, type=Path, help="常用箱登记表.xlsx")
    parser.add_argument("--photos-dir", required=True, type=Path, help="原始照片文件夹")
    parser.add_argument("--output-dir", required=True, type=Path, help="输出目录")
    parser.add_argument(
        "--mapping",
        type=Path,
        help="可选CSV，表头必须为：样品号,照片1,照片2",
    )
    parser.add_argument("--batch-id", help="可选批次号；默认使用当前时间")
    parser.add_argument(
        "--max-xlsx-mib",
        type=float,
        default=16,
        help="每卷目标大小，默认16MiB，绝对不得超过20MiB",
    )
    parser.add_argument(
        "--max-samples-per-volume",
        type=_positive_int,
        default=DEFAULT_MAX_SAMPLES_PER_VOLUME,
        help="每卷最多样品数，默认200",
    )
    parser.add_argument(
        "--max-edge",
        type=_positive_int,
        default=DEFAULT_MAX_EDGE,
        help="压缩图最长边像素，默认1600",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=_positive_int,
        default=DEFAULT_JPEG_QUALITY,
        help="JPEG质量，默认76",
    )
    parser.add_argument(
        "--allow-unused",
        action="store_true",
        help=(
            "未使用配对CSV时，允许照片文件夹中存在未配对图片；"
            "使用CSV时，未写入CSV的照片默认忽略"
        ),
    )
    parser.add_argument(
        "--result-json",
        type=Path,
        help="把结果写入 UTF-8 JSON 文件；供 Windows 图形启动器稳定读取中文",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_product_photo_workbooks(
            template_path=args.template,
            photo_dir=args.photos_dir,
            output_dir=args.output_dir,
            mapping_path=args.mapping,
            batch_id=args.batch_id,
            strict_unused=not args.allow_unused,
            target_xlsx_bytes=round(args.max_xlsx_mib * 1024 * 1024),
            max_samples_per_volume=args.max_samples_per_volume,
            max_edge=args.max_edge,
            jpeg_quality=args.jpeg_quality,
        )
    except ProductPhotoBatchError as error:
        payload = {
            "ok": False,
            "message": str(error),
            "report_path": str(error.report_path) if error.report_path else None,
            "issue_count": len(error.issues),
            "issues": [
                {
                    "code": issue.code,
                    "sample_id": issue.sample_id,
                    "row_number": issue.row_number,
                    "filename": issue.filename,
                    "message": issue.message,
                }
                for issue in error.issues
            ],
        }
        _write_result_payload(payload, args.result_json)
        return 2
    payload = {
        "ok": True,
        "batch_id": result.batch_id,
        "sample_count": result.sample_count,
        "photo_count": result.photo_count,
        "source_photo_bytes": result.source_photo_bytes,
        "compressed_photo_bytes": result.compressed_photo_bytes,
        "formal_import_status": result.formal_import_status,
        "volumes": [
            {
                "volume_number": volume.volume_number,
                "volume_count": volume.volume_count,
                "path": str(volume.path),
                "size": volume.size,
                "sha256": volume.sha256,
                "sample_ids": list(volume.sample_ids),
            }
            for volume in result.volumes
        ],
        "report_csv": str(result.report_csv),
        "report_json": str(result.report_json),
    }
    _write_result_payload(payload, args.result_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
