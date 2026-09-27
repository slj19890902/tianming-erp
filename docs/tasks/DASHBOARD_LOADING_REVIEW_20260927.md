# DASHBOARD_LOADING_REVIEW_20260927

## Scope and authorization
User requests verification of recent updates and investigation/fix of dashboard loading. Baseline: origin/factory-current-baseline 01d90a7e; installed v0.22.512 source d9cdcb06 (recheck before release).

## Sequence / acceptance
1. Read-only logs and isolated consistent SQLite backup; profile dashboard query counts and identify repeated work.
2. Fix evidenced bottleneck without changing metric eligibility, permissions, customer scope or business facts; bounded frontend loading and stale-session protection.
3. Focused dashboard correctness/performance tests plus latest Excel/pre-delivery/mobile QR regressions. Record uncovered gaps separately.
4. Required release gates, verified backup, signed release and read-only acceptance. No production page automated clicks; admin manual acceptance remains distinct.

## Invariants
Preserve Tianhua image recognition, existing latest features and unrelated worktree edits. Fixtures/attachments only in isolated tests; no formal import/shipment/data mutation. No migration planned. Single agent.
