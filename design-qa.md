# UI-UNIFIED-NAV-20261008 visual verification

The previous baseline QA report is preserved in `docs/release_reports/DESIGN_QA_BASELINE_20a53068.md`.

final result: passed

Source visual truth: `D:/.codex/workspace_artifacts/erp-unified-nav-preview-20261008/统一导航-仓库预览.png` (1810 × 869 pixels).
Implementation: `D:/.codex/workspace_artifacts/erp-unified-nav-preview-20261008/implementation-warehouse-final-reference-size.png` (1810 × 869 pixels, CSS viewport 1810 × 869, deviceScaleFactor 1).
Also captured 1920 × 960, 1366 × 768 and narrow 760 × 800. No image rescaling was used for the matching-size comparison.
State: standard display, isolated administrator, 3F E racks, G1 elevation open. The source is a generated layout reference; its business rows are illustrative. Implementation uses a current isolated database copy with actual shelf stock. Formal database was not used for browser actions.

## Findings and comparison history

1. Initial full-view comparison (`implementation-warehouse-r1.png`, source image opened together in one comparison input) showed unbordered inactive top navigation, unlike the selected bordered controls. P2. Added the existing ERP border/background tokens. The initial map showed the whole floor; it was not used to judge rack proportions.
2. Approval interaction initially replaced the embedded workspace, leaving the outer navigation disconnected. P1. Replaced the footer action with an ordinary permission-filtered new-tab link; this preserves the original ERP frame and user activation. `chrome-tools.json` verifies the separate approval target and subsequent inventory navigation.
3. Final source and implementation were opened together at 1810 × 869 with G1 selected, along with focused captures `implementation-header.png` and `implementation-tools.png`. No remaining actionable P0/P1/P2 mismatch in the approved navigation scope.

## Required fidelity surfaces

- Fonts and typography: original Chinese system font stack retained, header navigation 14 px, standard sidebar 14 px; large mode navigation/sidebar 17 px. Source generated text is somewhat larger/bolder; preserving production standard/large sizing is intentional. No clipped navigation or footer labels. Real account name replaces the illustrative boss account.
- Spacing and layout: 64 px global header; 216 px persistent desktop sidebar; scrollable business menu and fixed footer; no visible outer work tabs or duplicate inner module header. Actual workspace top is y=64 at both 1920 and 1366 widths. Original warehouse canvas/rack split remains its responsive business layout; reference's artificial rack proportions are not imposed on it.
- Colors and tokens: existing white/light-blue surfaces, blue selected controls, thin cool borders. Map age and stock colors remain unchanged. Active and hover styles are consistent between module families.
- Image quality and assets: actual Tianming brand PNG retained, no reconstructed logo. Existing map renderer retained. Footer icons use the existing Element Plus icon package. No new bitmap assets or custom illustration approximations.
- Copy/content: flow sequence and warehouse/finance/master/system labels match the approved plan. Permission-filtered extra entries remain under More. Warehouse metrics remain available in a compact top-right popover; this intentional position keeps the complete metrics without changing the nested map toolbar.

## Verification evidence

`chrome-modules.json`: 15 module/view states, 1920 and 1366 large mode; no horizontal document overflow, footer inside viewport, original topbar hidden, outer tabs absent. `chrome-tools.json`: separate approval page, workspace preservation, refresh, password form, draft action guard, narrow sidebar, logout; zero script exceptions in final normal run.
History/cache check used real Chrome text input in a disposable password form, browser forward/back, and verified the input remained; canceled without submitting a password change. A preliminary DOM-value-only test did not emit input and was corrected; it is not counted as a product failure.
An artificial randomUUID-removal probe initially affected every nested document and exposed the unchanged map bundle's dependency; it was discarded as a visual run. The new navigation helper is independently tested with getRandomValues only (no randomUUID). Normal final Chrome runs have no script exceptions.
Browser extension was unavailable. Verification used a separate headless Google Chrome profile through its local DevTools interface against `http://127.0.0.1:18569` only. No IAB, Playwright CLI/MCP, or production page automation.

## Implementation checklist

- [x] Unified header and footer on all main business modules.
- [x] Preserve original permissions, workflow filters, inventory guard and attached frame cache.
- [x] Supported handshake required before suppressing original navigation.
- [x] Validate message source, active frame, actor/generation and command allowlist; deduplicate commands.
- [x] Build and static resource reference verification; 12 behavior tests and 4 adjacent warehouse tests.
- [x] Final full-view and focused visual comparison after fixes.
- [ ] Administrator's formal workstation acceptance remains external; physical printing and mobile receipt screens were not changed.

Follow-up polish: none required for the approved scope. Existing business tables retain their own dense layouts; this change unifies the system navigation rather than replacing every business form.
