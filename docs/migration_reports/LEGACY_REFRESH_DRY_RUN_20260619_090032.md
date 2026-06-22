# Legacy Ruida Refresh Dry-run Report

- Batch ID: `legacy-ruida-20260619_090032`
- Mode: `dry-run`
- Database modified: `no`
- SQL Server: `.\BOXERP/BoxDB20_REPRO`
- SQLite: `D:\纸箱厂erp软件搭建\data\sandboxes\carton_erp_legacy_refresh_apply_test_20260619_085901.sqlite3`

## Complete log

```json
{
  "batch_id": "legacy-ruida-20260619_090032",
  "started_at": "2026-06-19T09:00:32.201418+08:00",
  "mode": "dry-run",
  "database_modified": false,
  "sql_server": ".\\BOXERP/BoxDB20_REPRO",
  "sqlite": "D:\\纸箱厂erp软件搭建\\data\\sandboxes\\carton_erp_legacy_refresh_apply_test_20260619_085901.sqlite3",
  "before": {
    "path": "D:\\纸箱厂erp软件搭建\\data\\sandboxes\\carton_erp_legacy_refresh_apply_test_20260619_085901.sqlite3",
    "size_bytes": 209182720,
    "modified_at": "2026-06-19T09:00:01.258743+08:00",
    "sha256": "BC72C2EE1778F7F181442FF21572C76792E2B4CB58D697280ACA119315DED118",
    "integrity_check": "ok",
    "table_counts": {
      "legacy_ruida_orders": 39922,
      "legacy_ruida_order_items": 40449,
      "legacy_ruida_customers": 132,
      "sales_orders": 4,
      "sales_order_items": 7
    }
  },
  "source_counts": {
    "orders": 39922,
    "items": 40449,
    "customers": 132
  },
  "diffs": {
    "orders": {
      "source_only_count": 0,
      "target_only_count": 0,
      "source_duplicate_keys": [],
      "target_duplicate_keys": [],
      "source_only_id_min": null,
      "source_only_id_max": null
    },
    "items": {
      "source_only_count": 0,
      "target_only_count": 0,
      "source_duplicate_keys": [],
      "target_duplicate_keys": [],
      "source_only_id_min": null,
      "source_only_id_max": null
    },
    "customers": {
      "source_only_count": 0,
      "target_only_count": 0,
      "source_duplicate_keys": [],
      "target_duplicate_keys": [],
      "source_only_id_min": null,
      "source_only_id_max": null
    }
  },
  "planned_inserts": {
    "orders": 0,
    "items": 0,
    "customers": 0
  },
  "customer_policy": "skip customers unless --refresh-customers is supplied",
  "existing_row_policy": "report only; never update existing legacy rows"
}
```
