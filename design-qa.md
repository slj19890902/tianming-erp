# Phase 2C-3 数字孪生全局视角与去标签视觉验收

## Comparison target

- source visual truth path (1F): `C:\Users\ADMINI~1\AppData\Local\Temp\codex-clipboard-b8e136b3-d97a-4c9b-a478-2500692bffe8.png`
- source visual truth path (3F): `C:\Users\ADMINI~1\AppData\Local\Temp\codex-clipboard-7d61eea7-c2bc-4aff-bd6b-57b8b54f0fd7.png`
- implementation URL: `http://localhost:8014/?page=warehouse&phase2c3=1`
- implementation screenshot path (1F): `D:\纸箱厂erp软件搭建-worktrees\factory-twin-editor-mvp\artifacts\phase2c3\implementation-1f-global.png`
- implementation screenshot path (3F): `D:\纸箱厂erp软件搭建-worktrees\factory-twin-editor-mvp\artifacts\phase2c3\implementation-3f-global.png`
- normalized comparison board (1F): `D:\纸箱厂erp软件搭建-worktrees\factory-twin-editor-mvp\artifacts\phase2c3\qa-compare-1f.png`
- normalized comparison board (3F): `D:\纸箱厂erp软件搭建-worktrees\factory-twin-editor-mvp\artifacts\phase2c3\qa-compare-3f.png`
- browser viewport: 1920 × 911 CSS px, devicePixelRatio 1; browser screenshot output 1904 × 904 px.
- source pixels: 1474 × 678 px for both reference images.
- implementation twin crop: 1593 × 737 px; normalized to 1474 × 678 px beside each source for the comparison boards.
- state: ERP embedded warehouse page, 1F/3F outer floor cards, 2.5D north-east global fit, operational layers enabled, object labels disabled.

## Full-view comparison evidence

Each comparison board places the source on the left and the normalized browser implementation on the right in one image. Both implementation views retain the source's dark navy technical shell, cyan grid, compact layer rail, orthographic 2.5D geometry and fixed right inspector. The 1F factory body fills the central stage without the projected column group outside the production workshop. The 3F warehouse uses the larger global fit shown by the source, with the complete measured warehouse remaining readable.

The implementation intentionally moves floor switching to the existing ERP floor cards above the twin instead of duplicating it inside the embedded command bar. It also intentionally removes all map label bars and projected object text at the user's request. These are current product requirements, not fidelity defects.

## Focused region comparison evidence

No additional pixel crop was required: camera fill, label density, southern column removal and aisle intersections are large, clearly readable surfaces in the full-resolution side-by-side boards. Interaction-focused verification was performed separately in Chrome: a 3F rack click opened `RACK-3F-F4-EAST-SOUTH-001` in the right inspector, and a 1F equipment click opened the locked water-based printing machine with real dimensions and coordinates.

## Required fidelity surfaces

- Fonts and typography: the existing Microsoft YaHei/Segoe UI Chinese UI hierarchy and Consolas technical micro-labels are preserved. Removing object sprites eliminates the small unreadable text cloud while keeping stage, scale, compass and inspector typography intact.
- Spacing and layout rhythm: the outer ERP floor cards now own floor switching; the embedded command bar reflows to three columns. 1F and 3F use floor-appropriate full-view framing and retain the fixed layer rail, map stage and inspector.
- Colors and visual tokens: navy panels, cyan grid, teal 1F aisles and amber 3F aisles stay within the source palette. Each floor's aisle network now uses one opaque color and one depth-writing surface so intersections do not darken or stack.
- Image quality and asset fidelity: the implementation continues to render measured Three.js geometry rather than a raster or placeholder approximation. No new decorative image asset was needed; the requested changes concern camera, visibility and semantic rendering.
- Copy and content: outer cards now say `一楼 生产车间 · 布局已接入` and `三楼 成品与半成品 · 已接入`. Object details appear only after clicking and remain tied to ERP/layout facts.

## Comparison history

### Pass 1 — blocked

- P2: 3F used the editor-scale global frustum and appeared materially smaller than the supplied 3F reference.
- P2: intersecting aisle segments retained mixed transparent colors, so overlaps looked like stacked strips rather than one connected route.

Fixes made:

- Increased the ERP warehouse frustum divisor while preserving the accepted 1F composition.
- Unified each floor's aisle color and changed warehouse aisle meshes to one opaque depth-writing elevation.

### Pass 2 — passed

- Post-fix `qa-compare-3f.png` shows the measured 3F layout occupying the intended global-view proportion.
- Post-fix `qa-compare-1f.png` shows the factory body without the non-workshop projected column group and without map label bars.
- No actionable P0, P1 or P2 visual differences remain. The outer white ERP floor-card strip, absence of internal floor buttons and absence of object labels are explicit user-requested changes.

## Primary interactions tested

- Existing ERP floor cards switched iframe URLs between `floor=1F` and `floor=3F`; the active state followed the selected floor.
- Embedded duplicate `.twin-floor-switch` count was 0.
- `编号标签` layer control count was 0 and the 2.5D screenshots contained no object label sprites.
- 1F 2D and 2.5D switched successfully; 2.5D was restored for handoff.
- 3F rack selection and 1F equipment selection both opened detailed right-inspector cards.
- Visible React error boundary count was 0 and all tested interactions completed. The connected Chrome surface does not expose a console-event stream; frontend type-check/build and browser interaction checks reported no application error.

## Follow-up polish

- P3: after formal rack-level inventory binding is approved, shelf tags can be introduced only inside the rack elevation view, keeping the global 2.5D view uncluttered.

final result: passed

---

# P0-29 仓库定位联动与合并信息精简 Design QA（2026-08-28）

## Comparison target

- Source: the four 1294×920 warehouse screenshots annotated by the owner in this task, covering the ERP warehouse shell, embedded command bar, toolbar and right inventory inspector.
- Implementation: v0.22.209 candidate assets built from `factory_twin/frontend` in the isolated worktree.
- Required viewport: 1294×920, matching the supplied screenshots.

## Implemented fidelity and interaction changes

- Floor switching no longer renders a transient pallet-size error while the requested floor's map and dashboard contracts are still loading; a settled, genuine contract mismatch remains fail-closed.
- Delayed-dispatch products have an always-available map focus action, with the permitted move action separated from inspection.
- Merge selection removes grey instructional paragraphs and internal pallet identifiers, uses one horizontal row of location and quantity chips, and highlights every selected map location in purple.
- The map palette uses pale green aisles, green empty locations, blue occupied locations, and purple selected or located results. The selection marker is a rectangular location highlight instead of a circular ring.
- Merge mode expands the right inventory inspector and raises the warehouse UI minimum readable text size to 12px.

## Automated evidence

- Node interaction tests: 91 passed.
- Python frontend and warehouse integration group: 75 passed.
- TypeScript `tsc --noEmit`: passed.
- Vite production build: passed; HTML and generated asset references are closed.

## Browser comparison status

- Chrome was selected as required by repository policy. The extension repeatedly timed out while claiming and reading the already-open warehouse tab, including after a fresh connection.
- The prohibited in-app browser was not used as a substitute. No formal database, runtime map or production service was changed for this QA attempt.
- Therefore the code and interaction gates pass, but the same-viewport visual comparison and click-through acceptance remain blocked until Chrome control is available or factory personnel perform the listed acceptance steps.

final result: blocked

---

# 编辑常用箱多箱型第二排布局修复 Design QA（2026-08-12）

## Comparison target

- baseline URL: `http://127.0.0.1:18128/`（v0.22.92 隔离验收副本）
- fixed implementation URL: `http://127.0.0.1:18129/`（当前独立修复工作树与独立 SQLite 副本）
- combined comparison board: `D:\tm-worktrees\erp-common-box-box-type-row-fix-20260812\.codex_tmp\box-type-row-qa\comparison-board.png`
- browser viewport and state: Codex in-app browser, 1606 x 979；新增常用箱；长宽高输入五位数、报料长宽输入六位数，具备三段压线的箱型切到“压线”。
- compared box types: 独立天盖、独立底、衬板、隔板、刀卡、模切内盒、异形箱。

## Required fidelity surfaces

- Layout rhythm: 保留 v0.22.92 五个紧凑功能色带和其余字段位置，只根据后端箱型能力给第二排选择标准箱、天地盖、三维带压线、三维、二维或无规格六种网格。
- Input capacity: 长宽高仍完整显示五位数字，报料长宽仍完整显示六位数字；二维衬板/隔板/刀卡不人为补空的“高”框。
- Business fields: 独立天盖、独立底、异形箱保留三段压线位置；衬板、隔板、刀卡、模切内盒保留可编辑“一开 N”；无拼箱、无舌头箱型没有幽灵占位。
- Frontend/backend consistency: 模切内盒和异形箱的后端 `required_dimensions` 均为长、宽、高；前端由同一规则源显示三个规格框，API 创建后读取三项值一致。
- Safety: 异形箱仍只允许人工填写报料尺寸，不新增自动展开公式；本轮没有迁移、没有正式数据库写入。

## Comparison history

### Pass 1 — fixed

- P1: v0.22.92 的固定九列网格假定所有箱型都有三规格、拼箱和舌头；条件字段被隐藏后，后续报料、开料、压线仍按原列号落位，造成文字和输入框相互堆叠。
- P1: 模切内盒只显示长宽，异形箱连成品规格框都不显示，与老板本轮确认的长宽高规则不一致。

Fixes made:

- 从后端箱型规则动态选择第二排网格，隐藏字段后其余字段自动紧邻排列，不再继承 A1 的空列假设。
- 模切内盒和异形箱改为长宽高三规格；保留人工报料边界，不引入未经确认的公式。

### Pass 2 — passed

- 七个指定箱型逐类浏览器测量，所有第二排字段矩形重叠数均为 0。
- 模切内盒、异形箱浏览器中均出现 3 个规格输入框；衬板、隔板、刀卡保持 2 个。
- A1、A3、围板、满摇盖、半开槽回归检查无重叠。
- 浏览器运行日志为空；相关自动化测试全部通过。

## Primary interactions tested

- 通过真实“主数据 → 常用箱与材质 → 客户 → 新增产品”路径打开编辑器。
- 逐项切换七个指定箱型并输入五位规格、六位报料；具备压线能力的箱型切换到“压线”显示三段尺寸。
- 对每个箱型读取实际 DOM 几何并检查字段交叠；七类均为 0。
- API 隔离数据库创建并重新读取模切内盒、异形箱，长宽高分别保持 `12345 / 23456 / 34567`。

final result: passed

# P1-34B3 65×45 mm 生产包装标签实尺寸极简版视觉验收

## Comparison target

- source visual truth path: `D:\.codex\visualizations\2026\08\11\019fee7c-7117-7761-871b-98acfc9f9c8d\production-label-real-size\source-65x45-1.png`
- implementation URL: `http://127.0.0.1:18112/production-packaging-label.html?id=1`
- implementation full-view screenshot: `D:\.codex\visualizations\2026\08\11\019fee7c-7117-7761-871b-98acfc9f9c8d\production-label-real-size\implementation-65x45-screen.png`
- implementation focused screenshot: `D:\.codex\visualizations\2026\08\11\019fee7c-7117-7761-871b-98acfc9f9c8d\production-label-real-size\implementation-65x45-first-label.png`
- normalized comparison board: `D:\.codex\visualizations\2026\08\11\019fee7c-7117-7761-871b-98acfc9f9c8d\production-label-real-size\comparison-65x45-source-vs-implementation.png`
- browser viewport: default in-app browser viewport, screenshot output 1280 × 720 px.
- source pixels: 563 × 390 px; implementation focused crop: 248 × 172 px. Both have the 65:45 physical aspect ratio and were normalized to the same 258 px comparison height.
- measured implementation label box: 245.656 × 170.078 CSS px, matching 65 × 45 mm at 96 CSS px/in within sub-pixel rounding.
- state: three realistic product labels loaded from the read-only production packaging label package; third label is the remainder bundle.

## Full-view comparison evidence

The browser full view shows all three labels at one consistent physical size. The source hierarchy is retained: customer first, product code as the strongest identifier, product name and specification in the middle, and bundle quantity as the bottom focus. The large black customer and quantity bands from the source are intentionally removed under the owner's latest requirement; the ERP version uses white paper, black text and thin rules only.

## Focused region comparison evidence

The side-by-side board compares one full 65 × 45 mm source label with one full ERP label at the same normalized physical ratio. After the first pass, the left-side field names were widened from 9 mm to 11 mm and forced to one line. The second browser measurement reports no horizontal overflow for any of the five field groups.

## Required fidelity surfaces

- Fonts and typography: Microsoft YaHei/SimHei with a clear black-and-white hierarchy; the inventory code and bundle count remain the fastest scanning targets.
- Spacing and layout rhythm: five compact rows fit inside the exact 65 × 45 mm border without overall scroll or clipping.
- Colors and visual tokens: label content uses white or transparent backgrounds only. Browser computed styles found no filled label region; no large black background is present.
- Image quality and assets: this label contains no decorative image or generated asset, which matches the requirement for a simple label-printer output.
- Copy and content: the printed label contains only 客户名称、存货编码、产品名称、规格、每捆数量. It omits barcode, production task number, order number, stock, location, cost and internal process facts.

## Comparison history

### Pass 1 — fixed

- P2: 客户名称 and 产品名称 could wrap into two lines in the 9 mm label-name column, weakening scanability.

Fix made:

- Increased the left label-name column to 11 mm and added `white-space: nowrap`.

### Pass 2 — passed

- Label box remains exactly 65 × 45 mm.
- All five labels stay on one line in the tested realistic sample set.
- Browser computed audit found zero horizontally overflowing label descendants, zero warning/error console messages, and no non-transparent background inside the label.
- No actionable P0, P1 or P2 visual issue remains.

## Primary interactions tested

- `重新加载` refetched the same GET-only preview and restored three labels without changing the URL.
- `打印包装标签` and `关闭` were enabled after successful loading.
- The print action was not invoked because it opens the operating-system printer dialog; CSSOM inspection confirmed `@page { size: 65mm 45mm; margin: 0; }`.
- Raw browser PDF printing is unavailable on this surface, so physical printer direction, gap/black-mark detection and 100% scale remain external human acceptance items.
- Browser console warning/error count: 0.

final result: passed

---

# Production task sheet design QA

- Source: `D:\纸箱厂erp软件搭建\output\pdf\生产任务单_内部生产版_三方案_A5.pdf`
- Source render used for A1 comparison: `D:\纸箱厂erp软件搭建\tmp\pdfs\render_internal\page-1.png`
- Combined source and implementation screenshot: `D:\tm-worktrees\erp-production-task-form-20260811\docs\qa\production-task-a1-comparison.png`
- Batch first-page screenshot: `D:\tm-worktrees\erp-production-task-form-20260811\docs\qa\production-task-batch-a4.png`
- Batch odd-last-page screenshot: `D:\tm-worktrees\erp-production-task-form-20260811\docs\qa\production-task-batch-last-page.png`
- Browser viewport: 1200 × 800. The comparison harness renders the ERP page in a 1200 × 1400 iframe scaled to 50% so the complete A4 state and the source render are visible together.

## Comparison

- Preserved the source hierarchy: title and status, three headline metrics, product and size row, ordered process route, technical details, and structure-reference column.
- Removed the source colors intentionally because the approved ERP output is black-and-white. Borders, type weights, and grayscale structure images retain the visual hierarchy without relying on color.
- The single-task state uses one full A4 page with enlarged type and a detail panel that fills the available height.
- The batch state places two fixed half-page cards on each A4 page. Three cards produce two pages and the final lower half remains blank.
- A1, die-cut inner box, and liner states were checked with realistic data. No text clipping, overlapping, broken borders, or layout-overflow marker was present.
- A real provided source image was loaded through the structure-reference image slot; the formal implementation uses the existing authenticated product/order drawing routes.
- The customer-safe state hides elements marked as internal and shows its safe-mode footer. The mode switch was exercised in the browser and restored successfully.

## Final result

passed

---

# 编辑常用箱最终紧凑布局 Design QA（2026-08-12）

## Comparison target

- source visual truth path: `D:\.codex\generated_images\019ff0a2-2505-76a3-be8b-d306cec3ed09\exec-fdfb8836-104f-4692-b1f1-ee943abde736.png`
- implementation URL: `http://127.0.0.1:18128/`
- implementation screenshot: `D:\.codex\visualizations\2026\08\12\common-box-final-layout\implementation-final.png`
- combined comparison board: `D:\.codex\visualizations\2026\08\12\common-box-final-layout\comparison-board.png`
- browser viewport and state: Codex in-app browser, 1606 x 979; isolated local UAT; A1/0201 product with material, die-cut mold/location, single-color printing, packaging label and price facts loaded.

## Required fidelity surfaces

- Fonts and typography: retained the ERP's existing Microsoft YaHei system stack and compact form hierarchy; labels remain readable without adding decorative typography.
- Spacing and layout rhythm: all ordinary A1 fields fit in one viewport. Customer and product name own the widest spaces; dimensions, report size, cutting mode, crease, material, production, drawing and price form five compact functional bands.
- Colors and visual tokens: light blue, cyan, lavender and warm-cream backgrounds separate structure, material, production and commercial/drawing facts while preserving the existing ERP component styles.
- Input capacity: box length/width/height visibly fit five digits, report length/width fit six digits, tongue fits two digits, and label quantity fits three digits without allowing native number-control chrome to hide entered values.
- Copy and content: A1 cutting mode is displayed as fixed text `一开一`; packaging label copy is shortened to `打印标签` and `数量`; internal BOM is retained inside a collapsed secondary disclosure.
- Backend fidelity: every visible editor field remains bound to the existing `productForm` save payload. Async material and mold options now repaint existing backend ids when they arrive, preventing stored values from appearing blank.

## Comparison history

### Pass 1 - fixed

- P1: the selected reference showed A1 cutting mode, but the implementation hid it because A1 has a fixed backend cutting mode rather than a configurable one.
- P1: an existing mold id could be stored by the backend while its search input remained visibly blank after asynchronous options loaded.
- P1: an existing material id had the same asynchronous label-repaint risk.
- P2: the long printing warning and recommendation hint consumed horizontal space and weakened the compact production row.

Fixes made:

- Always show the cutting-mode field; fixed-mode box types render read-only `一开一`, configurable types retain the existing normalized numeric input.
- Added an options watcher to the shared SearchSelect so existing backend ids repaint their labels after material or mold options load.
- Reduced the printing warning to `按图纸核对` while retaining the full warning in the title tooltip, and clamped the recommendation hint to one line.

### Pass 2 - passed

- The combined reference/implementation board preserves the approved information architecture and functional grouping.
- Browser save/reopen verified that a five-digit length, six-digit report length and three-digit label quantity remain visible and are returned by the backend.
- Material and mold/location labels are visible after a clean reload from saved backend ids.
- No actionable P0, P1 or P2 visual mismatch remains. The reference's example values and drawing-history sample differ from the isolated UAT fixture by design and do not alter the layout contract.

## Primary interactions tested

- Opened the common-box editor through the real master-data navigation and selected a real isolated UAT product.
- Switched through the full modal state with material candidates, mold-required production, printing method, label strategy and collapsed BOM present.
- Saved and reopened the product after entering length `54321`, report length `123456` and label quantity `321`; all three values returned visibly from the backend.
- Verified the material summary and `M-0201-08 | 模具008 | 1F-M-R01-L2-G01` binding repaint on a clean load.
- Automated frontend contract/regression suite and API round-trip test passed.

final result: passed

---

## Current task final result

blocked
