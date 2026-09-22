"""Read server-generated immutable SVG files; never accept uploaded SVG markup."""
import hashlib
import base64
import json
import os

from fastapi import HTTPException
from app.services.secure_uploads import resolve_stored_reference


def release_paper_snapshot(release) -> tuple[dict, dict]:
    """Same immutable figure payload for planned and historical receipt paper."""
    if not os.getenv('ERP_FILE_STORAGE_DIR'):
        raise HTTPException(503, '图纸存储根未显式配置，不能生成生产纸单')
    try:
        pdf = resolve_stored_reference(release.pdf_reference)
        if not pdf.is_file() or hashlib.sha256(pdf.read_bytes()).hexdigest() != release.pdf_sha256:
            raise HTTPException(503, '发布PDF缺失或校验失败，不能生成生产纸单')
        manifest = json.loads(release.manifest_json)
        image_assets = {}
        for index, obj in enumerate(manifest['print_objects']):
            if obj.get('kind') != 'image' and not obj.get('asset_reference'):
                continue
            asset = resolve_stored_reference(obj['asset_reference'])
            if not asset.is_file() or asset.stat().st_size > 5_000_000:
                raise HTTPException(503, '发布印刷图片缺失或超过限制')
            content = asset.read_bytes()
            if hashlib.sha256(content).hexdigest() != obj['asset_sha256']:
                raise HTTPException(503, '发布印刷图片校验失败')
            if obj.get('original_reference'):
                original = resolve_stored_reference(obj['original_reference'])
                if not original.is_file() or hashlib.sha256(original.read_bytes()).hexdigest() != obj['original_sha256']:
                    raise HTTPException(503, '发布印刷原件缺失或校验失败')
            obj['data_uri'] = f"data:{obj['asset_mime']};base64,{base64.b64encode(content).decode('ascii')}"
            image_assets[index] = (content, obj['asset_mime'])
        layers = {}
        for layer in ('structure', 'print'):
            rendered = read_svg_snapshot(manifest, layer)
            if rendered is None:
                from app.services.drawing_geometry import svg
                rendered = svg(manifest['geometry'], manifest['print_objects'] if layer == 'print' else [], image_assets)
            layers[layer] = 'data:image/svg+xml;base64,' + base64.b64encode(rendered.encode('utf-8')).decode('ascii')
        return manifest, {'release_id': release.id, 'product_id': release.product_id,
            'number': release.external_number, 'revision': release.revision,
            'geometry': manifest['geometry'], 'print_objects': manifest['print_objects'],
            'svg_urls': layers,
            'pdf_url': f'/api/master/products/{release.product_id}/managed-drawing/releases/{release.id}/file'}
    except (OSError, KeyError, ValueError) as error:
        raise HTTPException(503, '发布图纸资料不可读，不能生成生产纸单') from error


def read_svg_snapshot(manifest: dict, layer: str) -> str | None:
    snapshots = manifest.get('svg_snapshots')
    if snapshots is None:
        return None  # Compatibility for releases created before SVG snapshots.
    if not os.getenv('ERP_FILE_STORAGE_DIR'):
        raise HTTPException(503, '图纸存储根未显式配置')
    ref = snapshots.get(layer)
    if not ref:
        raise HTTPException(503, '发布图示快照缺失')
    try:
        path = resolve_stored_reference(ref['reference'])
        if not path.is_file() or path.stat().st_size > 40_000_000:
            raise HTTPException(503, '发布图示快照缺失或超过限制')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != ref['sha256']:
            raise HTTPException(503, '发布图示快照校验失败')
        return content.decode('utf-8')
    except (OSError, UnicodeDecodeError, KeyError, ValueError) as error:
        raise HTTPException(503, '发布图示快照不可读') from error
