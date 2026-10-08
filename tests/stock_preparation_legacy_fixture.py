"""Pre-upgrade receipts retain the explicit/manual planning regression suite.

These tests deliberately model receipt facts created before automatic planning.
The new production receipt path is exercised in test_stock_processing_auto.py.
"""
import pytest
from test_stock_replenishment_flow import stock_replenishment_app as base_stock_replenishment_app


@pytest.fixture
def stock_replenishment_app(base_stock_replenishment_app,monkeypatch):
    from app.services import stock_preparation_processing
    monkeypatch.setattr(stock_preparation_processing,'auto_plan_receipt',lambda *args:None)
    return base_stock_replenishment_app
