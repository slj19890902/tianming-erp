# Legacy Ruida Refresh Dry-run Report

- Batch ID: `legacy-ruida-20260619_083126`
- Mode: `dry-run`
- Database modified: `no`
- SQL Server: `.\BOXERP/BoxDB20_REPRO`
- SQLite: `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`

## Complete log

```json
{
  "batch_id": "legacy-ruida-20260619_083126",
  "started_at": "2026-06-19T08:31:26.629952+08:00",
  "mode": "dry-run",
  "database_modified": false,
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
  "existing_row_policy": "report only; never update existing legacy rows"
}
```
