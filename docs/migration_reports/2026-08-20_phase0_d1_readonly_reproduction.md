# 2026-08-20 Phase 0 D1 readonly reproduction

## Result

| 指标 | 阶段 0 时点 | 新副本 |
|---|---:|---:|
| unfinished orders | 79 | 78 |
| unfinished items | 250 | 255 |
| received without task | 53 | 53 |
| trace interruptions | 54 | 54 |
| posted completion gaps in current unfinished scope | 27 | 21 |

Counts are a new snapshot, not fixed expectations. A count leaving the unfinished scope does not prove that its historical inventory trace was repaired.

## Classification

- received-without-task subtypes: `{"legacy_status_no_task": 41, "normalized_receipt_no_task": 12}`
- received-evidence task partition: `{"all_waiting_material": {"item_count": 1, "order_count": 1}, "completed_or_not_required": {"item_count": 86, "order_count": 40}, "no_task": {"item_count": 53, "order_count": 16}, "pending": {"item_count": 33, "order_count": 8}}`
- current completion-gap subtypes: `{"direct_delivery_only_without_inventory_trace": 21}`
- all-history posted completion trace gaps: `36`

The received-evidence partition is a state inventory, not an assertion that every received item lacks finished stock. Pending/completed/not-required states are not silently upgraded to errors.

## Read-only proof

- package: `factory-20260820-144653-e7994493`
- package Git SHA: `e7994493562b99dd564ab4221f9045ecf22da829`
- database revision: `vv30v8x9z19`
- database SHA-256 before/after: `3e1ba7fbce2d8be2ab76b926af2c2282d046c61130404680f9b4d3024f07dd91`
- size: `219447296` bytes
- mode/query_only: `ro/1`
- write probe denied: `true`
- total_changes before/after: `0/0`
- quick_check/foreign keys: `ok/0`

## Dry-run boundary

- No apply mode exists and no business row was changed.
- Candidate keys and business identities are anonymized in the JSON companion.
- Every candidate is marked `automatic_apply_allowed=false`.
- Any future repair requires a new factory-side authorization, live precondition recheck, one-object transaction, audit reason and rollback on mismatch.

## Manual acceptance

1. Factory reviewers map anonymized candidates to restricted identifiers locally.
2. Confirm normalized receipt source before considering task creation; legacy-status-only rows remain manual trace reviews.
3. Reconcile component quantities before changing any waiting-material projection.
4. For completion gaps, confirm physical disposition, delivery and inventory allocation; do not manufacture stock merely because a historical trace is absent.
5. Open a separate authorized repair/migration task if schema or formal data writes are required.
