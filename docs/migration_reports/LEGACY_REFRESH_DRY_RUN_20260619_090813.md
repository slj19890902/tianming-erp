# Legacy Ruida Refresh Dry-run Report

- Batch ID: `legacy-ruida-20260619_090813`
- Mode: `dry-run`
- Database modified: `no`
- SQL Server: `.\BOXERP/BoxDB20_REPRO`
- SQLite: `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

## Complete log

```json
{
  "batch_id": "legacy-ruida-20260619_090813",
  "started_at": "2026-06-19T09:08:13.385714+08:00",
  "mode": "dry-run",
  "database_modified": false,
  "sql_server": ".\\BOXERP/BoxDB20_REPRO",
  "sqlite": "D:\\纸箱厂erp软件搭建\\data\\carton_erp.sqlite3",
  "before": {
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
