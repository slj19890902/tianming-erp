"""Package the bundled EasyOCR version's verified Simplified Chinese models."""
import ast
import hashlib
from pathlib import Path
import shutil


def prepare_readonly_layout(model_directory: Path):
    # EasyOCR creates both directories even with downloads disabled. Keep its
    # custom-network import directory in the signed, immutable code tree too.
    directory = model_directory.parent / 'user_network'
    directory.mkdir()
    (directory / '.keep').write_text('Signed empty EasyOCR user-network directory.\n', encoding='ascii')


def copy_models(source: Path, site_packages: Path, destination: Path):
    tree = ast.parse((site_packages / 'easyocr/config.py').read_bytes())
    required = {'craft_mlt_25k.pth', 'zh_sim_g2.pth'}
    expected = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        values = {key.value: value.value for key, value in zip(node.keys, node.values)
                  if isinstance(key, ast.Constant) and isinstance(value, ast.Constant)}
        if values.get('filename') in required and values.get('md5sum'):
            expected[values['filename']] = values['md5sum']
    if set(expected) != required:
        raise ValueError('随包 EasyOCR 缺少已知中文模型校验信息')
    for name, digest in expected.items():
        path = source / name
        if not path.is_file() or path.is_symlink() or hashlib.md5(path.read_bytes()).hexdigest() != digest:
            raise ValueError('离线中文识别模型缺失或校验失败：' + name)
    destination.mkdir(parents=True)
    for name in sorted(required):
        shutil.copyfile(source / name, destination / name)
        if hashlib.md5((destination / name).read_bytes()).hexdigest() != expected[name]:
            raise ValueError('离线模型复制校验失败：' + name)
    return sorted(required)
