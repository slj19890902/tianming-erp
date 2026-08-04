# 工厂数字孪生布局编辑器 MVP · Phase 2A

这是一个与正式 ERP 数据隔离的 React + Three.js / FastAPI 子应用。Phase 2A 在 DXF 底图和人工设备布局之上增加货架、堆放区域、通道、禁放区、图层与简单规则检查，不读写库存、订单、生产、流水或正式库位数据。

## 本地运行

```powershell
$env:FACTORY_TWIN_EDITOR_TOKEN='local-mvp-token'
python -m factory_twin.backend
```

另开一个 PowerShell：

```powershell
Set-Location factory_twin\frontend
npm install
npm run dev
```

打开 `http://127.0.0.1:5174`。后端默认监听 `127.0.0.1:8092`，开发数据库为 `factory_twin/data/factory_twin.sqlite3`。

## Phase 2A 功能

- 设备可“确认并锁定”，锁定后后端拒绝移动、旋转、改尺寸和删除。
- 参数化货架支持毫米长宽高、层数、格数、正面方向、最小通道宽度和 90° 旋转。
- 区域、通道和禁放区使用毫米坐标点保存；2D 与 2.5D 共用同一数据源。
- 区域支持“地面堆放 / 货架上层 / 架空共享”垂直语义。货架上层区域可与下方设备共享平面投影，只有真实高度区间相交时才报冲突；2.5D 会按离地高度显示立体存放层。
- 同一布局重复绘制时会按类型自动生成下一个可用编号，例如 `ZONE-1F-RAW-001` 后自动使用 `ZONE-1F-RAW-002`，不会覆盖既有对象。
- AI 来源的对象只能创建为 `candidate`，必须通过人工确认接口才能成为 `confirmed`。
- 简单规则检查覆盖货架占通道、堆放区压设备/柱子、通道宽度不足和消防出口遮挡。
- 内置模拟纸箱厂底图、三台生产设备、模具货架、堆放区、叉车通道和消防出口禁放区。

## 数据边界

- DXF 原文件只在单次请求中只读解析，解析后临时文件立即删除。
- 数据库只保存源文件名、SHA-256、派生结构、设备/货架素材元数据、布局坐标和候选/确认状态。
- 外墙、墙体和柱子标记为锁定；所有 DXF 结构都没有修改接口。
- 旋转角度只允许 0/90/180/270，坐标和尺寸统一为毫米。
- 货架上层的离地高度和占用高度必须按现场实测录入；界面给出的 2500 mm 只是录入起点，不是照片推算结果。
- 默认编辑令牌仅用于本机 MVP；部署或接入 ERP 前必须改为正式登录、角色和审计门禁。

## 自动化验证

```powershell
python -m pytest tests\test_factory_twin_mvp.py tests\test_factory_twin_phase2a.py -q
Set-Location factory_twin\frontend
npm run build
```
