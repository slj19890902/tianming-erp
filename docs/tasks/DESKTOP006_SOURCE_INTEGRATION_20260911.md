# DESKTOP-006 独立助手代码纳入正式代码库
当前持续完整交付授权范围。基线 ee620ac0/v353/sb11v8x9z76。差异仅 desktop_assistant、其测试和任务说明；不修改 app/static/alembic、依赖或现有服务脚本。此闭环只将已验证独立安装更新工具纳入正式 Git，不启动助手、不接管工厂、不注册23点任务、不调整业务运行版本v353或数据库。不将代码整合称为助手已投产。
正式时点副本 D:/tm-uat/installer-code-integration-20260911/before-code-integration.sqlite3，SHA b9d1a8be40040d94de813efea24e19f5296c3e2c28a4eb2d296e19a1bd76485d，integrity=ok/FK=0/head唯一。此前助手35项组合测试及新增加密签名备份8项定向测试通过；不用工厂重复全量测试。
正式分支必须干净、与实时origin一致，候选必须包含该基线，使用ff-only；验证运行资产无差异、原PID2648保持、LAN健康及Git同步。回退只撤销新增工具源码，不恢复旧业务库。管理员验收仍使用NAS先读我，正式接管另需本机恢复口令/NAS设置。
