# GROUPLOC001 整组生产选位与内网提交

状态：开发验证通过，待合并 v381 与正式发布；待生产取消涉及实物完成口径，尚未变更该状态规则。

范围：生产安排默认三楼，按实际区域名称 A-Z / 子区 / 货架 / 层 / 格级联选择。内部 EDIT / L 区域编码不作为大类；仍提交正式 location_id，区域、批次与货架身份不重建。计划成品位置右侧增加地图选位，默认三楼二维；有货或空的正式货位均可确认，返回保留套数。计划选位不移动实物、不自动记完工。

原因：旧 production-workspace.js 提交在 try 之前调用 crypto.randomUUID，厂内 HTTP 上方法不可用，抛异常且未显示错误，POST 未发送。已用相同旧代码在隔离环境复现 `crypto.randomUUID is not a function`。现用 HTTP 可用的 crypto.getRandomValues 生成128位操作编号，编号创建纳入错误处理；同载荷不确定失败重试沿用编号，改载荷重新编号；提交中禁止重复。

接口：temporary-locations 仅增加已有 map_rack_id/rack_code/rack_display_name/level_no/slot_no 读取字段。地图生产选位为只读模式，消息核对 origin、iframe window、随机会话token、当前用户世代及原对话框身份，再重读当前可用位置；失效/非法位置不带回。不绕过实际提交时布局版本、容量、权限和数量门禁。

验证：7项备库/组合后端，20项Node（HTTP提交、重复/幂等、选位消息、层级、旧订单返回/货架定位/模板），TypeScript/Vite；隔离Chrome使用合成数据、本地真实Vue页面和仓库bundle，覆盖无randomUUID提交、有货/空货架格、实际空格点击、返回位置与套数、地图无写请求。正式页面不自动点击，发布后由管理员验收。

无新migration，无数据库或业务事实回填；205每网格套长3短4、一箱5套保持。发布需实时正式基线、Manager全程锁、备份/隔离演练/唯一head/完整性/FK、健康/静态只读核对及签名完整安装包；v381优先，本任务预计v382。
