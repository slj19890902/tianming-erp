from __future__ import annotations

from threading import RLock, local
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api import warehouse as warehouse_api
from app.services import warehouse_twin_layout_editor as editor


class _TrackingRLock:
    def __init__(self) -> None:
        self._lock = RLock()
        self._local = local()
        self.enter_count = 0
        self.exit_count = 0

    @property
    def held_by_current_thread(self) -> bool:
        return getattr(self._local, "depth", 0) > 0

    def __enter__(self) -> _TrackingRLock:
        self._lock.acquire()
        self._local.depth = getattr(self._local, "depth", 0) + 1
        self.enter_count += 1
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.exit_count += 1
        self._local.depth -= 1
        self._lock.release()


def test_floor4_calibration_holds_layout_lock_through_commit_failure_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _TrackingRLock()
    events: list[str] = []
    restored: list[object] = []
    snapshot = object()

    class FailingSession:
        def commit(self) -> None:
            assert lock.held_by_current_thread
            events.append("commit")
            raise RuntimeError("simulated audit commit failure")

        def rollback(self) -> None:
            assert lock.held_by_current_thread
            events.append("rollback")

    def take_snapshot() -> object:
        assert lock.held_by_current_thread
        events.append("snapshot")
        return snapshot

    def calibrate(*_args: object, **_kwargs: object) -> editor.LayoutMutation:
        assert lock.held_by_current_thread
        events.append("calibrate")
        return editor.LayoutMutation(
            value={
                "recalibrated": True,
                "inventory_changed": False,
                "calibration": {},
                "freight_elevator": {},
            },
            floor_revision="recalibrated-revision",
            applied=True,
        )

    def audit(*_args: object, **_kwargs: object) -> None:
        assert lock.held_by_current_thread
        events.append("audit")

    def restore(value: object) -> None:
        assert lock.held_by_current_thread
        events.append("restore")
        restored.append(value)

    monkeypatch.setattr(
        warehouse_api, "WAREHOUSE_TWIN_LAYOUT_TRANSACTION_LOCK", lock
    )
    monkeypatch.setattr(
        warehouse_api, "snapshot_warehouse_twin_layout_draft", take_snapshot
    )
    monkeypatch.setattr(
        warehouse_api, "calibrate_floor4_freight_elevator", calibrate
    )
    monkeypatch.setattr(warehouse_api, "_twin_layout_asset_log", audit)
    monkeypatch.setattr(
        warehouse_api, "restore_warehouse_twin_layout_draft", restore
    )

    payload = warehouse_api.TwinFloor4FreightElevatorCalibrationPayload(
        expected_revision="current-floor4-revision",
        operation_key="floor4-transaction-lock-001",
        source_points=[[0.0, 1500.0], [2000.0, -1500.0], [2000.0, 1500.0]],
        confirmed=True,
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/warehouse/twin-layout/floors/4F/draft/calibrate-freight-elevator",
            "headers": [],
            "client": ("testclient", 50000),
        }
    )

    with pytest.raises(RuntimeError, match="simulated audit commit failure"):
        warehouse_api.calibrate_twin_floor4_freight_elevator(
            "4F", payload, request, FailingSession(), SimpleNamespace(id=1)
        )

    assert events == [
        "snapshot",
        "calibrate",
        "audit",
        "commit",
        "rollback",
        "restore",
    ]
    assert restored == [snapshot]
    assert lock.enter_count == 1
    assert lock.exit_count == 1
    assert lock.held_by_current_thread is False
