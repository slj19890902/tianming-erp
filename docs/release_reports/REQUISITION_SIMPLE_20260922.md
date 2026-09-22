# 合并报料六列简化（2026-09-22）

状态：技术发布完成，待管理员人工验收。当前任务为老板“按照建议执行优化任务”，只改合并报料主流程。

## 效果
主表为报料长×宽、压线、材质/楞型、库存抵扣、本次报料、备注六列；常规只填写报料数量和备注。来源、需求换算、开料与用途分配折叠；采购尺寸默认只读，管理员特殊调整仍在详情，已有预占时禁止改尺寸。全部安全组/整组抵扣继续一次事务，不恢复逐批操作。
本次报料少于需求显示待报料缺口，多于需求自动计入材料备库。全额库存抵扣后保留明确填写的额外备料与备注，零张不生成采购。高级开料调整保留额外备料。历史已印刷/客户范围、楞型/精确尺寸、并发/事务/幂等门禁不变。

## 实时基线与隔离
正式 v0.22.480 / ae3d1b4f782003d9a2a7a63150c5be955579c438，运行 D:/TianmingERP，正式库 shared/data/carton_erp.sqlite3，du0920。
从实时 origin/factory-current-baseline e7eda8c8a86d89e0af289fcb933e12772989e288 建立 D:/tm-worktrees/requisition-simple-20260922，分支 codex/requisition-simple-20260922。原工作区无关修改保留。
隔离浏览器库 D:/tm-uat/requisition-simple-20260922-browser/semi-order-b1.sqlite3，端口18124，独立配置。没有对正式页面自动点击。

## 真实测试
- `python -m pytest tests/test_requisition_simple.py tests/test_requisition_group_pool.py tests/test_p1_80_purchase_purpose_frontend.py tests/test_p1_80_purchase_purpose_allocation.py -q --tb=short`：76 passed，139.98秒；122条既有测试依赖/短测试密钥警告。日志 D:/tm-worktrees/requisition-simple-final-tests.log。
- `node tests/requisition_group_pool_ui.cjs`：通过；总需求、共享栈板、缺口、精确尺寸、单请求、备注/额外备料、开料保留备料。
- `node tests/check_index_vue_template.cjs`：完整 Vue 模板编译通过。
- Python compileall、唯一 Alembic head dv0922、git diff --check：通过。
- 隔离真实 Chrome：224+112片需求、334+100片两批库存；六列表头及两个常规输入；300张提示缺36、386张提示备50；一键抵扣336后保留50及备注。关闭测试浏览器后通过真实接口恢复预览，再点击正式采购按钮，HTTP201生成50张采购；无Vue运行错误。截图/脚本 D:/tm-uat/requisition-simple-20260922/{before,after,details,saved}.png、chrome.cjs、chrome-save.cjs。
- 后端含真实收料：额外50张进入材料备库，没有额外生成50只成品；重复保存回放同回执；失效预览和伪造用途拒绝。

## 迁移及数据保护
新增 dv0922（父du0920），仅放宽用途快照“权威订单需求张数”从>0为>=0，其他守恒约束保留，允许库存全覆盖但仍有明确额外材料采购。无历史行改写。
正式库只读隔离副本 du0920→dv0922→du0920→dv0922：298业务表逐行哈希相同，192触发器定义相同，integrity ok、FK0。证据 D:/tm-uat/requisition-simple-20260922/migration-result.json。
已有零需求备料事实时拒绝降级，禁止强退破坏业务事实。旧包保留；正式升级使用助手签名包、验证NAS备份、停服隔离演练、事实哈希核对与健康门禁。

## 未验证及人工验收
未做正式页面自动点击、未代替管理员人工验收；未跑无关全系统回归。实际 token 用量无法获取。
1. 打开报料采购，勾选可合并订单，点击合并报料；核对六列、展开来源与高级调整。
2. 对合格库存点击整组一键抵扣，核对只剩缺口；不足/额外数量直接改本次报料，查看提示。
3. 填备注保存，核对正式采购数量与备注；有额外备料时确认其保留。

## 正式发布回执
- 2026-09-22 v0.22.481，应用提交 3790e536f0737e729c85780ad1341d07f4240404；codex/requisition-simple-20260922 与 factory-current-baseline 已推送。
- 签名包 ee7476e7b59d8a354451587cdf068b28a8a34b33deebf4ad96ded22da85df7ef，NAS latest已核验，发布身份公钥指纹 ce14118c44f5b84d45cc84cecbb8b70a6319ea2323b084577299a91b843c0cb2。未显示、复制或导出私钥。
- 正式程序切换至 D:/TianmingERP/releases/ee7476e7b59d8a354451587cdf068b28a8a34b33deebf4ad96ded22da85df7ef；同一正式数据库路径未替换，约束升级du0920→dv0922。
- 已验证NAS完整时点备份：Z:\sata1-18015598002\BoxERP\backups\20260922-084553-04eb4052.tmbackup；SHA256 41aa288b8febff678bb756b4c564a95a3a519d528ec80333b6bca3ccd315fa5b。
- 助手在备份后隔离演练通过，再执行正式迁移；迁移期间298张业务表数据逐项一致。重启后额外整体比较最初因邮件服务自动更新email_intake_settings.last_sync_started_at/last_sync_completed_at未通过，随后逐列核对确认仅这两个运行时间戳变化，其余历史业务事实不变，未回填库存、订单、材质、颜色或成本。
- 正式健康：127.0.0.1:18000、192.168.3.80:8000、172.16.1.26:8000以及HTTPS外网入口均200且ok=true；未登录版本/待报料接口401；正式HTML与签名包一致，SHA256 987b581435fb895069514524bc11d55b8461842b672dcb4b2d4e032b8f5df6f3。完整性ok、FK0、唯一revision dv0922。
- 保留上版包a2d0f110...与NAS备份，不自动回写旧库。隔离18124测试服务已停止，测试证据保留。
- NAS独立回执：Z:/sata1-18015598002/天明ERP知识库/04_开发记录/任务回执/20260922-报料六列简化与备料保留-v481.md。
