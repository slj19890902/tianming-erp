# 1F 真实 DXF 货梯区与统一 2.5D 设备设计 QA

- source geometry: `D:\Downloads\#TM_FACTORY_1F_ERP_V4_CONFIRMED_OUTDOOR.dxf`
- source SHA-256: `226e97de6a9ce51cc8d41486d08d385853623f1f73e726ca903b6a4a80e39836`
- source visual references: `D:\Downloads\Gemini_Generated_Image_*.png`（老板提供的 9 张机器参考）
- combined comparison: `C:\Users\Administrator\.codex\visualizations\2026\08\03\019fc691-bab4-77f0-8cd1-a881b5c51006\factory-map-design-qa-comparison.jpg`
- implementation screenshot: `C:\Users\Administrator\.codex\visualizations\2026\08\03\019fc691-bab4-77f0-8cd1-a881b5c51006\factory-map-1f-rack-priority-uat.png`
- rack front screenshot: `C:\Users\Administrator\.codex\visualizations\2026\08\03\019fc691-bab4-77f0-8cd1-a881b5c51006\factory-map-rack-front-uat.png`
- browser: Chrome connector, isolated UAT on port 18084
- state: 仓库库存管理 -> 库位管理 -> 1F -> 货梯旁一层货架 -> 第 1 层第 1 段

## Geometry evidence

不再使用上一轮的手工猜测覆盖物。新 DXF 的实际实体已固定为：

- `B9A`：上方货运电梯，停用；
- `B9B`：下方货运电梯，使用中；
- `B97`：货梯旁新增的开槽老虎机；
- `B98`：与开槽老虎机投影重合的一层货架；
- `B99`：开槽老虎机下方可临时放 2 托的绿色区域。

旧猜测的两部货梯、货架、开槽机、两个单托栈板框和两条通道覆盖物已全部撤销。入口缓冲区①
恢复为 DXF 原有区域，不再被错误隐藏。

## Visual fidelity evidence

- 9 个透明 WebP 设备资产均从老板提供的参考图生成，保留机器可识别轮廓和主要结构；
- 所有资产统一为等距 2.5D 技术线稿、深灰轮廓、蓝灰/工业绿主体、少量安全橙点缀和柔和阴影；
- 新水性印刷机、旧印刷机、分纸机、打包机、两种钉箱机、半自动粘箱机、模切机和开槽老虎机均为独立资产；
- 大、中、小模切机使用同一机型视觉语言，但由三个 DXF 占地框分别决定显示体量；
- 11 台设备图片全部成功加载，未出现白底方块或损坏图标。

## Layering and interaction evidence

- 固定机器始终没有 button 角色和点击事件，只记录位置并提示禁止堆放栈板；
- 绘制优先级为机器 4、货架 5，货架形状和标签始终位于机器之上；
- `B97` 开槽老虎机透明度为 `0.46`，只作为货架下方定位参考；
- `B98` 货架可点击，Chrome 实测打开 1 层、横向 4 段的正视定位图；
- Chrome 实测选择“第 1 层 · 从左第 1 段”正常回显；
- 临时栈板区为一个真实绿色框，容量 2 托，不拆成两个伪造库位，也不生成正式库位。

## Comparison history

1. 上一候选：依据截图猜造货梯与通道位置，并隐藏入口缓冲区①。状态：不通过。
2. 本候选第一次：改用新 DXF 的 `B97` 至 `B9B` 实体，撤销全部猜造覆盖物。状态：几何通过。
3. 本候选第二次：用 9 个独立透明线稿资产替换 4 个通用图片，并加入货架优先层级与低对比机器定位图。状态：视觉与交互通过。

## Findings

- 无 P0/P1/P2 视觉、几何或交互问题。
- P3：Chrome 连接器当前窄视口截图中文字较密；地图在实际桌面宽窗口会按现有响应式布局放大，
  不影响货架点击和设备识别。

## Regression evidence

- 1F 地图显示 11 台设备、2 部货梯、6 个货架、1 个临时栈板区；
- 货梯旁货架为可点击 button，设备仍不可点击；
- 1F / 3F 来回切换正常；三楼 396 个启用货位保持不变；
- 自动测试：`19 passed`；未增加任何库存写入路径。

final result: passed
