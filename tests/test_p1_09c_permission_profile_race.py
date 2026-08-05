from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_permission_editor_separates_list_profile_and_save_busy_states() -> None:
    for marker in (
        "permissionUsersLoading: false",
        "permissionProfileLoading: false",
        "permissionSaving: false",
        'permissionError: ""',
        'permissionSaveError: ""',
        ':disabled="permissionUsersLoading || permissionProfileLoading || permissionSaving"',
        ':disabled="permissionProfileLoading || permissionSaving || !permissionProfile',
    ):
        assert marker in INDEX


def test_permission_profile_only_latest_selected_user_can_render() -> None:
    for marker in (
        'const requestKey = "permissions:profile";',
        "const requestedUserId = Number(userId);",
        "this.beginLatestRequest(requestKey)",
        "signal: controller.signal",
        "latestRequestControllers.get(requestKey) !== controller",
        "Number(this.selectedPermissionUserId) !== requestedUserId",
    ):
        assert marker in INDEX


def test_permission_profile_failure_clears_stale_editor_and_exposes_retry() -> None:
    assert 'this.permissionError = `权限配置读取失败：${this.errorMessage(error)}`;' in INDEX
    assert "this.permissionProfile = null;" in INDEX
    assert "this.permissionDraft = { permissions:{}, customer_access_mode:\"all\", customer_ids:[] };" in INDEX
    assert '@click="loadPermissionProfile(selectedPermissionUserId)"' in INDEX
    assert "重新读取" in INDEX


def test_permission_save_snapshots_target_and_rejects_repeat_submit() -> None:
    for marker in (
        "if (this.permissionSaving || !this.permissionProfile) return;",
        "const userId = Number(this.permissionProfile.user_id);",
        "Number(this.selectedPermissionUserId) !== userId",
        "const payload = {",
        "customer_ids:[...this.permissionDraft.customer_ids]",
        "this.permissionSaving = true;",
        "axios.put(`/api/auth/users/${encodeURIComponent(userId)}/access`, payload)",
        "if (Number(this.selectedPermissionUserId) === userId)",
        "this.permissionSaving = false;",
    ):
        assert marker in INDEX


def test_permission_save_failure_keeps_editor_and_draft_visible_for_retry() -> None:
    assert 'this.permissionSaveError = `权限保存失败：${this.errorMessage(error)}`;' in INDEX
    assert '<div v-if="permissionSaveError" class="status red"' in INDEX
    assert "this.permissionProfile = null;" not in INDEX.split("async savePermissionAccess()", 1)[1].split("openCreateUserModal()", 1)[0].split("catch (error)", 1)[1]


def test_permission_controls_are_locked_while_save_is_in_flight() -> None:
    assert ':disabled="item.locked || permissionSaving"' in INDEX
    assert INDEX.count(':disabled="permissionSaving"') >= 3
    assert ':disabled="permissionProfileLoading || permissionSaving || !permissionProfile' in INDEX


def test_permission_frontend_inline_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend contract test")
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
