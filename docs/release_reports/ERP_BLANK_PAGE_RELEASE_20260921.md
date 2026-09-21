# ERP 首页空白修复正式发布回执（2026-09-21）

状态：已正式发布 v0.22.471，待管理员人工验收。

## 原因与修复
v0.22.469 送货列表重打按钮的嵌套三元表达式缺少一个右括号，使整页 Vue 模板编译报 Unexpected token ':'，页面无法挂载。ERP 助手仅核对 HTTP/HTML 和后端健康，所以仍显示“网页可访问”；本次未修改助手检测逻辑。
修复 static/index.html 的括号，并新增 tests/check_index_vue_template.cjs 全模板编译检查，接入 tests/test_p0_37_vue_template_browser_safety.py。app/version.py 升为 v0.22.471。保留正式分支已有 v0.22.470 看板缩放修复。

## 真实验证
修复前：完整模板编译复现失败；修复后通过。
命令：node tests/check_index_vue_template.cjs
命令：D:/纸箱厂erp软件搭建/.venv/Scripts/python.exe -m pytest -q tests/test_p0_37_vue_template_browser_safety.py tests/test_p1_09c_37_delivery_action_guards.py tests/test_p1_09c_39_return_receipt_action_guards.py tests/test_p1_09c_42_delivery_reprint_guard.py
结果：14 passed in 2.04s；git diff --check 通过；alembic heads 为唯一 du0920。
隔离 Chrome http://127.0.0.1:18121/static/index.html 实际呈现登录表单。本验证为独立静态服务，不连接业务数据库，不代表已验证登录和全部业务流程。
正式发布后：部署目录完整模板编译通过；127.0.0.1:18000、192.168.3.80:8000、172.16.1.26:8000 的 /api/health 均200且ok=true；正式HTML散列等于签名清单。数据库只读 integrity_check=ok、foreign_key_check=0、revision=du0920。

## 发布与边界
分支 codex/erp-blank-page-20260921；代码提交 7f01087b87766b0bd1039c38385fd956304eb6ea；已推送并快进正式分支 factory-current-baseline。
无迁移。通过助手 Manager.update 完成签名验证、停服完整备份、备份解密与NAS散列验证及重启；旧程序保留可回退。未改写正式订单、库存、材质、颜色或成本；未覆盖业务数据库；归档工作区他人改动保留。

发布包：8baed7be20064ab7b49d7d0dc15d011cf6a188a0f89fb2796a1ffdd23f06eaf8
备份：Z:\sata1-18015598002\BoxERP\backups\20260921-085344-8f4fb047.tmbackup
备份 SHA256：e36c20f0d40c77223daf10eb228ca90d54a86d30238ebf9d2170da8d6a3d6b20

## 三步人工验收
1. 打开 http://192.168.3.80:8000/，按 Ctrl+F5，确认显示登录界面或业务首页。
2. 正常登录，打开报料页面，确认页面和列表显示。
3. 打开送货列表，确认操作按钮正常显示；需要实际重打时由管理员选择业务单据确认。
未执行正式页面自动点击，未宣称人工验收通过；本轮仅修复首页空白，不代替此前库存任务的全部合同验收。
