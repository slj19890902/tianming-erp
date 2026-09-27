# PRE_DELIVERY_INBOUND_20260927
User authorizes fixing v512/v513 pre-delivery in-transit diagnostic. Baseline origin/factory-current-baseline 2a4d23ab; running v513 caca767e, ec0927xl (recheck before release).

Success: count only effective frozen procurement quantity not received/short-closed/voided, preserve conversion to customer units and purchase purpose; aggregate multiple allocations without reusing shared stock. Recompute diagnostic when viewing existing Excel batch and changing allocations, not only upload. Unknown/complex source facts explicitly require review instead of inventing coverage. Preserve Tianhua image recognition, quantity/permission/customer/state/CAS/idempotency/transaction/audit checks. No automatic import or dispatch, no migration or business backfill.

Sequence: inspect authoritative receiving/purpose/conversion services; add failing isolated tests for status-only, partial purchase/receipt, short close/void/reverse, conversion, multiple orders and source selection; implement shared read-only diagnostic and UI live refresh as required; focused regressions; signed release with verified backup, integrity/FK and read-only acceptance; NAS receipt; administrator final page acceptance.

Allowlist: new pre_delivery_readiness service; tianhua_pre_delivery service/API and relevant frontend if needed; focused tests; version and task/release docs, short handoff index. Single agent; original worktree unrelated changes preserved.
