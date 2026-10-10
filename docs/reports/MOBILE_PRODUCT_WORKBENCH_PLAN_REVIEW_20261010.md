# 手机产品工作台可靠性：独立实施前复核

2026-10-10。只读审核，尚未对未冻结候选执行验收；本报告不是正式发布回执。独立审者只写本 review 目录及 NAS，不修改仓库、正式数据或进程。

## 基线与依据

- 根源码 `4df10c8a0c4f1d35fafff36b59079bc410af4375`，工作树干净；根任务卡给出的正式基线为 v0.22.606 / `4b66fc4c104cf49bc48b065c20b5aeed17f9403a`。正式运行状态由根负责核验，本审者未请求正式业务接口。
- 本轮已依次读取 CODEX_START、NAS AI_START、本 TASK、PLATFORM、总需求相关章节及执行章程 3–9。单位问题另精读总需求第 10 章：组合前零件按片显示，单位名称修正不得换算数量或改写冻结台账。
- 复用 `api/REPORT-AUDIT.md` 的 3 个唯一合成观测节点和 `ui/REPORT.md` 的 11 个实际实现观察；这些是故障复现及正常控制，不称修复已通过。API 是 2 类根因，UI 是 5 类根因，变体不重复计数。

## 结论与实施门槛

已授权最小方案没有架构阻断。下列门槛应在精确候选和真实 HTTP 交叉验证中同时成立。

1. **单位仅作实物显示投影。** 已有 goods profile `output_piece=true` 的 processed_component 组、位置、扁平 items 和实际批次 reverse 均为片。普通片料仍为张，finished 保留工艺/冻结单位合同。不得改库存数量、预占、版本、lot.unit、匹配资格、换算或跨客户绑定。原共享 `_inventory_group(unit='片')` 只改组头，位置仍继承非成品硬编码张；应在本 workbench 局部修正，不动共享 helper。
2. **整数与分页按实际绑定边界。** product_id/customer_id/known_customer_id/lot_id 不能超出 SQLite signed int；分页要核 `(page-1)*page_size`，不能只限定 page 后乘法仍溢出。非法输入为可读 4xx；缺省可选参数、正常缺档 404、既有权限与范围维持，尺寸精度合同不收紧。
3. **手机图纸按明确展开加载。** 结果与详情默认均无预览请求或实际 preview src；复用现有 `TmProductDrawings.append/disposeWithin`。切产品、页签重绘和销毁只清理当前工作台宿主，取消请求并释放 blob；不修改共享图纸组件，不把当前产品图纸替代历史任务冻结图纸。桌面既有展示保持。
4. **请求及后续动作同轮核验。** get 的响应检查不足以保护 `await show()` 后的历史页签与 initialState 恢复。响应应用、错误、finally、页签 continuation 都要匹配当前实例、产品及请求轮次。账号变化通过现有 destroy/remount 边界隔离；旧请求 401 不得注销当前新账号。
5. **草稿与已提交参数分离。** 表单 input/change 保存草稿；提交、分页与重试使用该次提交快照，迟到响应或图纸重试不覆盖尚未提交的输入。反查真实 lot_id 和 processed_state 身份不能被重绘或表单默认值悄悄改变。
6. **有限等待与坏回包可重试。** timeout 需结束 busy 并保留原请求条件；即使 request 不遵守 abort，迟到成功/失败仍不能覆盖新轮次。null、错误对象或关键包络缺失的 2xx 不得显示空库存/空产品、崩页或当成成功。允许真实 permission-hidden、停用品、无附件及合法空 items；校验应按 search/reverse/detail 包络分别进行。

## 权限和生命周期对照

API 真实审查已证明：受限用户不能查看其他客户产品；未绑定跨客户 lot 404；明确 allowed-product 后可反查，但原 owner 隐藏且候选仅当前范围。修复应保留这些事实，不能为单位修复移除绑定或增加跨客户曝光。

手机入口 `clearMobilePortalState` 与 logout 调用 destroyMobile；setPage 先 disposeWithin(document.body)，返回 lookup 时同一用户 mountMobile 可能复用实例。现有图纸 toggle 会重新登记生命周期，因此候选需证明返回仍能再次展开，而非扩改共享组件。离线 linkedom 的 DOM/src/fetch stub 证据不冒充真实屏幕、网络时序、Cookie 或浏览器验收。

## 相邻待确认项

原 reverse_source 可含 `processed_state=output_piece`，而原反查 select 只有 raw/creased/printed/die_cut。首次“查用途”请求可正确，再次提交表单可能降为 raw 并被实际批次资格拒绝。已请 UI 负责人用现有实际方法夹具最短确认；根允许确认后在同一实物反查合同内补选项。此时仅为具体源码风险，不先登记新增已证实 Bug，不扩大匹配算法。

## 最终验证范围

待 API/UI 精确 SHA 后，仅独立执行最短 API 单位/范围/边界和真实 HTTP→实际 UI，检查 mobile 展开前零预览、切产品/账号迟到、未提交草稿、timeout/坏 2xx、合法空/隐藏包络。复用作者已完成正常搜索、地图返回、桌面与首页相邻回归，不重复大组。保留每项最后有效证据来源及源码指纹；浏览器/管理员现场验收继续单列 pending。无 Chrome、服务、PID、正式业务写入或部署动作。

本轮一次只读定位误写不存在的 `static/ui/product-drawings.js`，随后以 rg --files 找到实际 `static/ui/mobile-product-drawings.js`；未影响仓库或业务，也不算产品缺陷。
