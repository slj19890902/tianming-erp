from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_delivery_list_uses_effective_receipt_quantity_projection() -> None:
    assert "deliveryDisplayQuantity(row)" in INDEX
    assert "item.display_quantity" in INDEX
    assert "item.original_delivered_quantity" in INDEX
    assert "item.quantity_difference" in INDEX
    assert "实际签收" in INDEX
    assert "原送货" in INDEX
    assert "差异" in INDEX


def test_delivery_list_keeps_existing_detail_refresh_after_receipt_mutations() -> None:
    assert "this.invalidateDeliveryListDetail(deliveryId);" in INDEX
    assert "await Promise.all([this.loadDeliveries(), this.loadOrders(), this.loadKpi(), this.loadFinance()]);" in INDEX
