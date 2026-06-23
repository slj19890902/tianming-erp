# Legacy Ruida Refresh Apply Report

- Batch ID: `legacy-ruida-20260619_090738`
- Mode: `apply`
- Database modified: `yes`
- SQL Server: `.\BOXERP/BoxDB20_REPRO`
- SQLite: `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

## Complete log

```json
{
  "batch_id": "legacy-ruida-20260619_090738",
  "started_at": "2026-06-19T09:07:38.271844+08:00",
  "mode": "apply",
  "database_modified": true,
  "sql_server": ".\\BOXERP/BoxDB20_REPRO",
  "sqlite": "D:\\纸箱厂erp软件搭建\\data\\carton_erp.sqlite3",
  "before": {
    "path": "D:\\纸箱厂erp软件搭建\\data\\carton_erp.sqlite3",
    "size_bytes": 209182720,
    "modified_at": "2026-06-15T17:32:39.673585+08:00",
    "sha256": "9CCECF883828A9CDAD54B44443765082E00969784EEDCFDE8F64812B59F18ADA",
    "integrity_check": "ok",
    "table_counts": {
      "legacy_ruida_orders": 39766,
      "legacy_ruida_order_items": 40293,
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
      "source_only_count": 156,
      "target_only_count": 0,
      "source_duplicate_keys": [],
      "target_duplicate_keys": [],
      "source_only_id_min": 42688,
      "source_only_id_max": 42843
    },
    "items": {
      "source_only_count": 156,
      "target_only_count": 0,
      "source_duplicate_keys": [],
      "target_duplicate_keys": [],
      "source_only_id_min": 43370,
      "source_only_id_max": 43527
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
    "orders": 156,
    "items": 156,
    "customers": 0
  },
  "customer_policy": "skip customers unless --refresh-customers is supplied",
  "existing_row_policy": "report only; never update existing legacy rows",
  "backup": {
    "path": "D:\\纸箱厂erp软件搭建\\data\\backups\\carton_erp_before_legacy_refresh_20260619_090738.sqlite3",
    "size_bytes": 209182720,
    "sha256": "9CCECF883828A9CDAD54B44443765082E00969784EEDCFDE8F64812B59F18ADA",
    "integrity_check": "ok"
  },
  "inserted": {
    "orders": 156,
    "items": 156,
    "customers": 0
  },
  "after": {
    "path": "D:\\纸箱厂erp软件搭建\\data\\carton_erp.sqlite3",
    "size_bytes": 209182720,
    "modified_at": "2026-06-19T09:07:42.318612+08:00",
    "sha256": "6ED950245BC91E18A8D4C8F55CB55B6630396B16117DF4145922C725E28E7F77",
    "integrity_check": "ok",
    "table_counts": {
      "legacy_ruida_orders": 39922,
      "legacy_ruida_order_items": 40449,
      "legacy_ruida_customers": 132,
      "sales_orders": 4,
      "sales_order_items": 7
    }
  }
}
```
