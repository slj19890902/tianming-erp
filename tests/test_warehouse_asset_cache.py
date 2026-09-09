from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def test_warehouse_entry_and_previous_assets_survive_release():
    source = (ROOT / 'app/main.py').read_text(encoding='utf-8')
    assert 'FileResponse(warehouse_twin_path, headers={"Cache-Control": "no-store"})' in source
    config = (ROOT / 'factory_twin/frontend/vite.config.ts').read_text(encoding='utf-8')
    assert 'emptyOutDir: false' in config
    for name in ('warehouseTwin-B9m6t90h.css', 'warehouseTwin-D9DRseS2.js'):
        assert (ROOT / 'static/factory-twin-assets/assets' / name).is_file()
    entry = (ROOT / 'static/factory-twin-assets/warehouse-twin.html').read_text(encoding='utf-8')
    for path in re.findall(r'(?:src|href)="(/factory-twin-assets/[^"?]+)"', entry):
        assert (ROOT / 'static' / path.lstrip('/')).is_file()
