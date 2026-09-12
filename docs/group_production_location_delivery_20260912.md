# GROUPLOC001 整组生产选位与内网提交

状态：v0.22.382 技术发布完成，待管理员人工验收。待生产取消涉及实物完成口径，用户选择尚待答复，本次不自动改变计划/实际完工规则。

范围：生产安排默认三楼，按实际区域名称 A-Z / 子区 / 货架 / 层 / 格级联选择。内部 EDIT / L 区域编码不作为大类；仍提交正式 location_id，区域、批次与货架身份不重建。计划成品位置右侧增加地图选位，默认三楼二维；有货或空的正式货位均可确认，返回保留套数。计划选位不移动实物、不自动记完工。

原因：旧 production-workspace.js 提交在 try 之前调用 crypto.randomUUID，厂内 HTTP 上方法不可用，抛异常且未显示错误，POST 未发送。已用相同旧代码在隔离环境复现 `crypto.randomUUID is not a function`。现用 HTTP 可用的 crypto.getRandomValues 生成128位操作编号，编号创建纳入错误处理；同载荷不确定失败重试沿用编号，改载荷重新编号；提交中禁止重复。

接口：temporary-locations 仅增加已有 map_rack_id/rack_code/rack_display_name/level_no/slot_no 读取字段。地图生产选位为只读模式，消息核对 origin、iframe window、随机会话token、当前用户世代及原对话框身份，再重读当前可用位置；失效/非法位置不带回。不绕过实际提交时布局版本、容量、权限和数量门禁。

验证：7项备库/组合后端，20项Node（HTTP提交、重复/幂等、选位消息、层级、旧订单返回/货架定位/模板），TypeScript/Vite；隔离Chrome使用合成数据、本地真实Vue页面和仓库bundle，覆盖无randomUUID提交、有货/空货架格、实际空格点击、返回位置与套数、地图无写请求。正式页面不自动点击，发布后由管理员验收。

无新migration，无数据库或业务事实回填；205每网格套长3短4、一箱5套保持。发布需实时正式基线、Manager全程锁、备份/隔离演练/唯一head/完整性/FK、健康/静态只读核对及签名完整安装包；v381优先，本任务预计v382。

## 正式发布

代码 `009793a0c37e56c1f021196a92abb89d8630b22f`，版本v0.22.382，唯一head mq0912；已合并v381最终文档ec3f0d60并重建，保留仓库分页与取消过期请求。正式计划 `D:\纸箱厂erp软件搭建\docs\migration_reports\release_runtime_20260912_165751.json` completed。

实际源 `D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3`，源与应用文件SHA256均 `2fa42e39335da07085451bee08d21216167663beb4b16c47cfd5ae8647e8a2b4`。备份 `D:\纸箱厂erp软件搭建\data\backups\carton_erp_before_release_20260912_165755.sqlite3` 与隔离演练 `D:\纸箱厂erp软件搭建\data\release_rehearsals\carton_erp_release_rehearsal_20260912_165755.sqlite3` SHA256均 `086d0a78d37a964228cfff8181b7ecdfcd81fd6591903ac73806ab4db9c6e45f`；integrity ok、FK 0、核心计数一致。没有新DDL，无订单/库存/位置历史事实回填。

持续Manager锁覆盖正式ff/Prepare/Apply/本机bootstrap替换，D:/TianmingERP原设置保持；运行时无managed marker或state，未执行首次接入。bootstrap回退 `D:\TianmingERP\control\installer-before-v382.zip`。

签名更新包SHA256 `4f1ce0380f8d20715f0994cc6eb421e12138dbaf10d6377c2f49f8089495d5a7`；完整Setup `52448bfa4a1aa2c6d910136e75c0940ad24974703a0eb040e5c8a57903a1e8ee`；复用已验证380助手 `0eab7f935211ff92df97b452b4899a8557f57b6f23ee1960e3817c4cb4acf5f7`。整包验签解压、Setup自检及嵌入release/助手字节核验通过。

人工最短验收：1. 内网生产整组安排查看三楼/区域/子区/货架层格；2. 地图点有货格和空格，确认返回原套数保持；3. 有实际业务时提交一组，检查成功或明确错误。正式页面未自动点击，不冒充现场验收。实际token用量不可获取。
正式只读验收：22项内网/本机健康、7个UI文件哈希、地图入口及依赖、生产/仓库未登录权限检查通过。zrok外网检查HTTPS读取超时，未冒称外网验收通过；本次不调整网络方案。NAS签名latest已382，完整Setup复制后SHA一致。本机bootstrap382已核验，原设置保持。
