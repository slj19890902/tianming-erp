# GROUP-ASSEMBLY-SAVE-RECOVERY-AUDIT-20261010

持续优化下一最小闭环，先只读审查并给方案，不凭相似代码直接修。正式与 NAS 已 v604，源 75129569ed5ff0bf454d075471c9b4fd902b070f，包 077c2cde0a2d54932987baa2a960aa5934f66dfb17194b273bd21051e9fa6081，en1009hp 无迁移；新冷备恢复、323表/附件保持、健康/资源通过，管理员验收 pending。根分支 codex/group-assembly-recovery-audit-20261010，仅比正式多文档。

开始沿 CODEX_START → NAS AI_START → 本卡 → ORDER_FLOW → 总需求7/14/17及章程3～9；已读未变内容复用。不得读全量交接/总需求/日志。会话不 push；不改正式业务数据或服务，不再尝试被拒的 Chrome/旧PID操作，不用 IAB。

## 最小对象

实际 group_stock“已组好整套入库”→ saveStockDialog('assemble') → POST group-actions/action=assemble。关注从已有真实加工子件组套的保存反馈、完整结果与原请求恢复；不是刚发布的 group_job/dispose，不重跑或改写该功能，不扩大 store_outputs、unassemble、single 输出或普通订单组套算法。

必须先证明页面和API实际链路，再分类缺陷/已有效门禁/未覆盖；不凑Bug数。多job同产品、部分组套余片、冻结配比与单位、原位置、预占、版本、权限、事务、幂等及审计保留。历史追溯不等于原保存键完整证明；未查到不等于原在途请求取消。

## 只读并行所有权

- API /root/mobile_drawings_api：复用其 clean group-save-recovery-api-20261010 树，固定 HEAD 4d6808e705b4155b998a5a2978e98c5fc4eee1d0，不切换/编辑源码。先比对受影响 API/服务/测试夹具与正式751候选字节或精确节点一致。所有新探针/报告仅写 D:/.codex/visualizations/2026/10/10/group-assembly-save-recovery-audit/api。合成 tmp_path 数据库，沿既有安全conftest runner；不得复制正式库。最短正常部分/全部组套、原key同body重放/异body拒绝、旧版本、前提交回滚/真实提交丢ack、原记录当前可查询性。可合并同一合成场景核库存/投入/成套产出，不扩跑全仓。导实际group_stock GET/sourceRow、原body、写/重放/查询、保存后workspace/history及原始头，保留证据来源SHA。
- UI /root/mobile_drawings_ui：复用 clean group-save-recovery-ui-20261010 树，固定 HEAD e578e90506fbcd654a6b2341fed75d447dcc8003，不切换/编辑源码，核三运行文件与正式751字节相等。探针/报告仅写同任务artifact/ui。使用实际HTML/mixin及API真实组库存投影；坏2xx/错误只作为传输故障输入，不能伪造成功proof。最短检查提交原body/key、空/部分成功回执、网络未知后改数/关闭重开/刷新、忙态与账号迟到401、现有dispose未知锁与新assemble保存的关系。没有真实成功JSON前不自创成功合同。复用已有Vue能力仅必要时做技术检查，不新增浏览器/服务/进程。
- 根：读两份完整报告，明确失败证据和保留的门禁，另定最小修复卡才开发。独立审者收到准确方案后复核；不与两个审查子任务抢源码。

## 交付

先每线少量高价值观察；证明相同根因后停止泛化参数。交 REPORT、精确源码指纹、真实HTTP/页面结果、过程错误与纠正、建议最小方案和NAS独立回执。API尽早给UI真实group_stock及正常assemble原文。本卡没有实施、迁移、停服、发布或清理授权。正式现状始终是v604，审查探针通过仅表示事实已验证，不代表任何新修复已上线。持续Goal保持active，目标仍为早上9点回顾。
