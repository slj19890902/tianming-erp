# Ruida Formal Sales Sample Migration

```json
{
  "mode": "apply",
  "sqlite": "D:\\纸箱厂erp软件搭建\\data\\sandboxes\\carton_erp_multi_item_formal_test_20260619_094449.sqlite3",
  "sha256": "1D0A35754D494886BAA48C615FFC0C71AB114AD1E303E37544D0CA73FCAB2815",
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
    "orders": 20,
    "items": 56
  },
  "before": {
    "sales_orders": 4,
    "sales_order_items": 7,
    "legacy_ruida_orders": 39922,
    "legacy_ruida_order_items": 40449
  },
  "after": {
    "sales_orders": 24,
    "sales_order_items": 63,
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
