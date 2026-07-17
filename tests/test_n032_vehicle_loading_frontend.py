from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_n032_vehicle_and_loading_api_contract_is_wired() -> None:
    assert 'axios.get("/api/deliveries/vehicles")' in INDEX
    assert 'axios.post("/api/deliveries/vehicles", payload)' in INDEX
    assert 'axios.put(`/api/deliveries/vehicles/${form.id}`, payload)' in INDEX
    assert 'axios.get(`/api/deliveries/loading-profiles/search?keyword=${encodeURIComponent(keyword)}`' in INDEX
    assert 'const productId = product.product_id || product.id;' in INDEX
    assert 'axios.get(`/api/deliveries/loading-profiles/${productId}`)' in INDEX
    assert 'axios.put(`/api/deliveries/loading-profiles/${form.product_id}`, payload)' in INDEX
    assert 'axios.post("/api/deliveries/loading-preview", {customer_id:this.deliveryForm.customer_id, vehicle_id:this.deliveryForm.vehicle_id || null, items})' in INDEX
    assert 'axios.post("/api/deliveries/loading-preview", {customer_id:row.customer_id, vehicle_id:row.vehicle_id || row.loading?.vehicle?.vehicle_id || null, items})' in INDEX


def test_n032_delivery_payload_and_dispatch_hash_confirmation_are_present() -> None:
    assert "vehicle_id:this.deliveryForm.vehicle_id || null" in INDEX
    assert "loading_confirmation:loadingConfirmation" in INDEX
    assert "expected_loading_hash:expectedLoadingHash" in INDEX
    assert "LOADING_HASH_EXPIRED" in INDEX
    assert "refreshDeliveryRowLoading(row)" in INDEX
    assert "装载计算已过期" in INDEX


def test_n032_loading_labels_and_non_blocking_disclaimer_are_present() -> None:
    assert 'normal:"正常"' in INDEX
    assert 'warning:"接近阈值"' in INDEX
    assert 'over_capacity:"超容"' in INDEX
    assert 'data_pending:"数据待补"' in INDEX
    assert "仅提示，不自动拆单" in INDEX
    assert "缺失产品参数" in INDEX
    assert "loadingRateText" in INDEX
    assert "loadingCoverageText" in INDEX


def test_n032_vehicle_and_profile_forms_are_visible_and_conditioned() -> None:
    assert "车辆与装载参数" in INDEX
    assert "v-if=\"canDelivery\" class=\"btn\" @click=\"openVehicleLoadingMaintenance\"" in INDEX
    assert "临时车号 / 未选择车辆" in INDEX
    assert "车牌自动显示" in INDEX
    assert 'value="theoretical_box"' in INDEX
    assert 'value="manual_unit"' in INDEX
    assert 'value="package"' in INDEX
    assert "v-if=\"loadingProfileForm.mode === 'manual_unit'\"" in INDEX
    assert "v-if=\"loadingProfileForm.mode === 'package'\"" in INDEX
    assert "按存货编码或产品名称搜索" in INDEX
    assert "全部客户" in INDEX
    assert "仅保存待确认" in INDEX
    assert "确认并用于装载计算" in INDEX
    assert "saveLoadingProfile(false)" in INDEX
    assert "saveLoadingProfile(true)" in INDEX
    assert "关键参数修改后旧确认自动失效" in INDEX


def test_n032_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend syntax validation"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "n032-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
