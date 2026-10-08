# ORDER_INVENTORY_RELIABILITY_20261008

## Authorization and objective

2026-10-08 owner request: further audit and upgrade order/inventory deduction reliability, produce a plan, assign suitable models, upgrade and publish. This continues the plan documented at NAS `20261008-报料生产简化与历史送货库存预警方案.md`. User confirmed historical remaining-stock colors (last 5 valid shipments) and automatic stock-production planning with actual component putaway. Current request explicitly authorizes implementation, multi-agent collaboration, commits and ordinary release after validation. No formal historical business-data rewriting is authorized.

Outcome: remaining-stock hints reflect future delivery needs; order previews/reservations/production use compatible physical identities and quantities; explicit product replenishment receipts enter processing automatically; component putaway is default and assembly remains actual physical work.

## Baseline and isolation

Live formal version v0.22.569, package b7452621084fee00ed7eb0ce7d0190bad621d9406711c20cf14dd9877119ecbb, source 11c4474b5f8b9fe5c7121a03f5867a57f786cb7a, previous compatible v568 package 683062a05f12ff84a8b14dd5321534ff46b4531bb51eb4a25037b90e46363c80. Recheck before release.
Remote factory-current-baseline is still 071cfa275b21d7ff536bcee6a6ea816a24e8cbd9 and does not represent running v569. Exception: start from verified running source plus its documentation-only commits, ed1228743f628ed91ab765d076f54055da7db0a9. No unrelated remote branch changes silently replace the deployed system. Git diff 11c4474b..ed122874 affects only design-qa and release/handoff documentation.
Integration worktree D:/tm-worktrees/order-inventory-upgrade-20261008; branch codex/order-inventory-upgrade-20261008. Codex managed worktree tools returned request errors; ordinary isolated Git worktrees used as fallback. Primary checkout dirty files preserved.
Formal database D:/TianmingERP/shared/data/carton_erp.sqlite3 must never be edited by development/tests. Use disposable test databases or protected isolated copies. No IAB or production browser automation. No historical data backfill. No migration expected; any need for one requires the full migration gates and one owner.

## Delivery plan

1. Read-only evidence-led audit: duplicate reservations, allocation across lines, preview/save disagreement, retries, cancellation/reversal, raw vs processed shape, cutting/mold units, BOM parent/child double credit. Turn confirmed defects into focused failing tests before fixes.
2. Extend existing server-owned inventory draft preview with final batch remaining stock and historical shipment references, not a parallel client allocator. Same customer/product/code, dispatched shipments, sum repeated lines per delivery before last-5 selection, freeze unit conversions, no samples means unknown. R>=max green; min<=R<max yellow; R<min red. Failures/unknowns do not become zero. Explicitly skipped stock must not change remaining-available into physical-on-hand.
3. Replace sidebar production entry with requisition while retaining production-only role access. Explain stock through physical available/allocated/remaining amounts and concise use actions.
4. Explicit product replenishment receipt creates/reserves a processing task exactly once; generic surplus/raw-purchase plans retain existing handling. Process each arrived component independently; record actual processing input/output and location; default semi/component output. Actual assembly consumes children and creates parent once. Keep existing receipt-reversal safety boundaries and ordinary order automatic flow.
5. Centralize critical material eligibility/physical-stage guards used by forward and reverse matching, freeze allocation yield and physical identity, distinguish source-board dimensions from real usable rectangles, protect actual material cost and customer scope. Do not replace sound existing logic with a wholesale rewrite.
6. Integration tests, independent review, isolated page checks, backup and verify, package/push/release, read-only formal health/assets, administrator manual acceptance steps, independent NAS receipt.

## File ownership

- root: integration worktree; static/index.html, static/ui/*, static/js/*, frontend-v2 source/build, frontend tests, task/report/requirements/version/release assets. Integrate agent commits; only root releases.
- order_inventory_reliability (GPT-6.1 Sol): isolated backend worktree; app/api/orders.py, matching/quantity services including semi_finished_inventory.py and sheet_cut_plan.py, new historical shipment reference service, scoped tests/fixtures. No stock_preparation* or frontend edits.
- stock_workflow_simplification (GPT-6.1 Sol): isolated stock worktree; app/services/stock_preparation*.py, app/api/stock_preparation.py, app/services/incoming_receipts.py and necessary new stock helpers/tests. No app/api/orders.py, sheet_cut_plan.py, semi_finished_inventory.py or frontend edits. Coordinate output identity contract with order agent.
- independent_review: read-only audit/review/test responsibility, no implementation edits.

## Invariants and acceptance

Ownership extension approved during concrete dependency review: order_inventory_reliability additionally owns processed_component_stock.py, stock_replenishment.py (coverage), bom_auto_reservation.py, multilevel_bom_requirements.py, multilevel_bom_inventory.py, bom_subkit_inventory.py, bom_pending_assembly.py, multilevel_bom_body_inventory.py. stock_workflow_simplification additionally owns bom_stock_pending.py. Root owns desktop_assistant/build.py, manager.py and targeted reader-contract tests: same Alembic revision does not authorize an older program to read newly introduced processed-component/processing/assembly facts. Signed reader capability and persisted activation are required, and unsafe previous code-only rollback is cleared before startup.

- Preserve permissions, customer boundaries, true product IDs, frozen BOM and procurement snapshots, transaction rollback, version checks, idempotency and audit.
- Preview is read-only; save reserves exactly once; actual processing consumes exact inputs; cancellations release only their own unconsumed reservation; delivery/reversal preserve source cost and quantities.
- Never count raw input and its processed output simultaneously or count consumed children alongside an assembled parent. Never multiply processed pieces by mold count again.
- Color tests: history 30/50/45 at 29,30,49,50; last5, same-document grouping, voided/pending exclusion, no/one sample, repeated lines/batch, explicit skip, mixed units and invalid frozen contracts.
- Stock tests: partial arrivals, independent child processing, actual partial processing, duplicate/retried submissions, stale versions, cancellation, assembly partial+leftovers, failures roll back input/output/cost together, no GET mutation of old stock.
- Existing cut tests previously stop at missing cost fixture. Repair legitimate test cost facts; do not weaken production cost gates.
- Publish only the reviewed coherent scope; runtime version, source SHA, assets and release report agree. Technical publication is not administrator acceptance.

## Exclusions

No permission relaxation, universal fuzzy matches, automatic customer/material substitutions beyond existing approved rules, guessing processed shape from bounding dimensions, silently migrating old physical stock, full ERP rewrite, or unrelated UI features. Unrelated failures found during audit must be recorded separately unless they directly block this flow.

## Concurrent baseline reconciliation

Release-blocking integration discovery: actual assembly from fully covered processed stock could still be rejected by the legacy material_status delivery gate. Root explicitly extends order_inventory_reliability ownership to app/api/deliveries.py and the minimum existing graph-readiness services needed to complete actual delivery and dispatch. Require real API regression through delivery creation/dispatch, exact stock consumption, insufficient-stock rejection and used-output reversal denial; do not manufacture receipt facts or weaken gates. This is part of the authorized order-stock-to-delivery closed loop.

2026-10-09 release recheck found v0.22.570 running source7538d3e0fdd15c8e30d470603bb729816a04410b, package a8b0823dc09ffb5493c8fb93653cbb8d8679a92916955f15bf1a5bd9f461a01f, same eg1008sc. It changes visible page capacity and shell size notifications. Merged verified running commit (703d5703), preserving both new stock scripts and deployed shell cache keys. Subsequent preflight uses this v570 baseline; target v571 subject to another live check. No business database replacement.
