# Ruida Formal Sales Sample Migration

```json
{
  "mode": "dry-run",
  "sqlite": "D:\\纸箱厂erp软件搭建\\data\\sandboxes\\carton_erp_multi_item_formal_test_20260619_094449.sqlite3",
  "sha256": "6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77",
  "limit": 20,
  "sample_mode": "multi_item_orders",
  "planned_orders": 20,
  "planned_items": 56,
  "selected_orders": 20,
  "selected_items": 56,
  "already_imported": 0,
  "skip_reasons": {
    "single_item_order": 11154,
    "product_unmatched_or_ambiguous": 128
  },
  "customer_matching": "legacy customer_id direct; unmatched order skipped",
  "product_matching": "customer_id + exact style_no to product_code/customer_material_code",
  "amount_total": "85388.10",
  "inserted": {
    "orders": 0,
    "items": 0
  },
  "before": {
    "sales_orders": 4,
    "sales_order_items": 7,
    "legacy_ruida_orders": 39922,
    "legacy_ruida_order_items": 40449
  },
  "after": {
    "sales_orders": 4,
    "sales_order_items": 7,
    "legacy_ruida_orders": 39922,
    "legacy_ruida_order_items": 40449
  },
  "integrity_check": "ok",
  "sample_legacy_order_ids": [
    39506,
    38511,
    37965,
    34014,
    33363,
    33190,
    33075,
    32797,
    32585,
    32549,
    32539,
    32417,
    32335,
    32168,
    32159,
    31873,
    31698,
    31603,
    31564,
    31489
  ]
}
```
