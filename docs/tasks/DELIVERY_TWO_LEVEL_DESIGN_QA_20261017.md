# Delivery two-level editor layout QA — 2026-10-17

final result: passed

Scope: selected layout and interaction requirements in the existing ERP, using actual changed Vue templates, methods and CSS mounted against illustrative data in isolated Chrome. This is not formal-site acceptance or proof of live backend writes. No production page was clicked. Backend contract and command regressions are verified separately.

Selected targets: exec-602a6dc0 (primary), exec-f0e2b88a (secondary), exec-c7525ca6 (expanded detail). Source images were inspected alongside implementation captures in one comparison call. Reference and matching captures use 1672×941 pixels; background application chrome in the generated reference is contextual and omitted from the isolated fixture. Existing ERP typography/button styling is retained, rather than recreating fictitious shell assets.

Implementation evidence: /Users/kimiyf/ERP-Migration/evidence/delivery-two-level-20261017/primary-1672.png, secondary-1672.png, details-1672.png, primary-1366.png, secondary-125-percent.png. Layout measurements in browser-layout-history.jsonl. Vue fixture reuses the changed production template/method source and layout module; API candidate responses and save/print transport in this visual fixture are illustrative. Real API behavior is tested by pytest and dispatch recovery tests.

Checks passed:
- Primary import directly above checkbox/sequence, secondary import immediately left of search field.
- Footer pagination precedes separate green save and orange print buttons. Visible at 1366×768 and a 1093×614 content viewport representing the available area at 125% scaling. The 1093 viewport measured print right=1053, bottom=580, fully inside the viewport. Temporary Chrome viewport override reset.
- Secondary ordered rows precede unordered stock; filled order quantity survives page change, stock quantity and edited price survive import, and successful import returns to primary. Order prices are plain text, stock prices are inputs.
- Continuous row numbering and red delete controls. Unit tests check deletion page clamping and rollback on a failed mixed-source import.
- Expanded detail has no specification/mold column. Quantity is immediately followed by price and amount. Blank drawings/categories remain blank.
- Actual measured expanded-column widths: PO136, inventory code112, name170, category48, drawing112, location220, quantity90, price90, amount100 pixels. All cell boundaries separate cleanly. Long values remain accessible by horizontal scrolling within the relevant cell/detail region; no text crosses a neighbor column.
- Main stylesheet loads after legacy inline style. Old misplaced print-only rules removed. Full actual Vue template compiles in the regression suite.

No unresolved P0/P1/P2 layout defects. Non-blocking differences: illustrative row counts and surrounding shell differ from the generated concept; actual ERP fonts and existing button shapes are preserved. Formal administrator acceptance remains pending after publication.
