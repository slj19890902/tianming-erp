# R07 v0.22.457 正式发布

技术发布完成，待管理员人工验收。R01–R07已技术发布；R08、R09继续，不冒充本轮全部完成。

代码 `fd8e8da8a800bf02b7e28be20d56e1379b80a52c`，分支 `codex/package-c-r05-r08-20260919` 已推送并快进 `factory-current-baseline`。签名包 `5aa7aa99ba1e3072132e84afa890832f76275661987573418cc28906f63186ab`，实际运行目录 `D:/TianmingERP/releases/5aa7aa99ba1e3072132e84afa890832f76275661987573418cc28906f63186ab`，入口 `runtime/python.exe -m desktop_assistant.server_entry`。

正式 DB `D:/TianmingERP/shared/data/carton_erp.sqlite3`，唯一 revision `up0919`，无迁移。发布不创建来料、库存、加工或送货事实。停服时点完整 NAS 备份 `Z:\sata1-18015598002\BoxERP\backups\20260919-223326-986d4b5a.tmbackup`，SHA256 `b08129ecf602297d0763ed9d4afeb3995326e25204e41f1b42b00b8dcdb8f233`；备份解密回读验证通过，停服 DB SHA `382633b1c337e5fbf1810a8712b5d38f9ef2feda6f629511f7378cc2df312232` 保持不变。原配置、地图和两份他人未提交文档保留。前版 v456 包保持可回退。

## 结果

专用多余盖/底保留客户、产品、部件、物理规格、压线开槽状态、方向、待打钉、来源和库位；默认不是成品或通用衬板。已有1盖的20套新单只买19盖和20底；收料不冒充打钉完成，实际加工确认才消耗原预占并入20套成品。Graph按真实子产品独立确认。请求幂等、过期摘要、审计故障回滚、受控撤销和真实/估算成本边界均保留。

## 真实验证

- `pytest tests/test_package_c_r07.py tests/test_p1_81_receipt_purpose_flow.py tests/test_bom_semi_production_frontend.py tests/test_multilevel_bom_receipt_flow.py tests/test_multilevel_bom_cost_lineage.py tests/test_package_c_r06.py -q`：83 passed，206.30秒。
- 补充后 `pytest tests/test_package_c_r07.py tests/test_bom_semi_production_frontend.py -q`：6 passed，14.67秒，含非管理员拒绝、真实备用采购成本来源和两种前端确认重试。
- 合入他人仅审计/测试的正式更新后 `pytest tests/test_audit_reserve_lineage.py -q`：1 passed，4.46秒。
- `npm run build`：tsc/Vite通过；内联JS `node --check`通过，`git diff --check`通过，Alembic唯一head up0919。旧JS非module/包大小提示及fixture短JWT密钥警告未隐瞒。
- 签名及29689个包内文件逐项摘要通过；三入口健康200、LAN首页/仓库新构建资源摘要、未登录订单和仓库401；正式库完整性ok、外键0。没有自动点击正式网页。

证据目录 `D:/tm-build457-r07-20260919`，NAS `BoxERP/desktop-assistant/release-v457-20260919`。测试入口 `http://127.0.0.1:18091/`，独立库 `D:/tm-uat/package-c-20260919/run/uat.sqlite3`，启动实际DB/写根/PID隔离验证通过。

## 管理员最短验收

1. 刷新正式订单/来料页。仅实际多余盖底时选专用未完工和真实方向，核对库存为待打钉。
2. 真实下一单采用后核对仅减少对应盖采购；来料后仍待加工，不冒充成品。
3. 现场实际打钉完成后，从订单明细“专用盖/底加工”预览并确认，核对原预占、成品和加工历史。

实体打印、手机、工厂屏幕及人工业务验收尚未代替；不要为验收伪造正式业务。
