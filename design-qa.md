# 送货单针式打印版式 Design QA

## Visual truth

- 目标样式：`D:\360MoveData\Users\Administrator\Desktop\1c18951ac48ba8fb8a2d27f4700bc879.jpg`
  - 原图：1280 × 1707 px
  - 对比采用第一张送货单区域：1280 × 820 px
- 修改前问题证据：`D:\360MoveData\Users\Administrator\Desktop\1fc951d15a83414a79513d94d7952efa.jpg`
- 最终单页 PDF 渲染：
  `D:\tm-uat\delivery_print_customer_prefix_20260724\evidence\delivery-print-TH-final-18091.png`
  - 1423 × 823 px，150 DPI
- 同屏归一化对比：
  `D:\tm-uat\delivery_print_customer_prefix_20260724\evidence\delivery-print-design-comparison-final-18091.png`
  - 左侧为老 ERP 目标，右侧为本轮实现。

## State and viewport

- 家庭隔离 UAT：`http://127.0.0.1:18091/static/delivery-print.html?id=10`
- 状态：天华客户、1 行明细、客户缩写编号 `TH-20260724-001`
- 浏览器视口：1256 × 856 px
- CSS 纸张：241 × 139.5 mm
- 实际 PDF：683.04 × 395.04 pt，1 页
- 页面测量：1 个 `.sheet`，`clientHeight=525`，
  `scrollHeight=525`，无纵向溢出。

## Visual checks

- 字体：公司名、标题、客户资料、表格、数量和签名区均按需求放大一级；
  中文无缺字、无裁切。
- 页首：公司名、标题、地址电话、客户资料和单号保持老 ERP 的紧凑横向结构；
  客户资料下方不再有横线。
- 表格：仅保留明细表自身边框；数量区、联次说明和普通签名项不再增加横线。
- 页脚：短单据时固定在纸张底部，与老 ERP 的视觉位置一致，不紧贴表格。
- 签名：`送货人 → 经手人 → 收货单位(签章)` 同一行；仅收货单位保留签章横线。
- 颜色与装饰：黑白针式打印样式，无阴影、图标或额外视觉装饰进入打印内容。
- 分页：正常 1 行单据实际 PDF 为 1 页；历史 11 行长备注单据按 6+5 行分为
  2 个有效页面，两页底部均未裁切，也未产生空白附加页。

## Fixes made during QA

- 首轮对比发现短单据页脚紧跟表格，与老 ERP 页脚位置不一致；改为
  `.sheet` 纵向 flex，并将 `.print-footer` 设为 `margin-top: auto`。
- 首轮打印测量与打印高度相差 0.5 mm；现已让屏幕分页探针和打印页统一使用
  `calc(var(--paper-height) - 0.5mm)`，消除临界行误判。
- 本地 PDF/HTML 证据含 UAT 副本资料；证据保存在 `D:\tm-uat`，仓库新增
  `tmp/` 忽略规则，禁止随候选提交。

final result: passed
