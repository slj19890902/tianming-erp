# PRODUCTION-COMPLETION-RESULT-AUDIT-20261010

持续 Goal 的下一最小只读闭环：核对桌面报料/生产中的备库“加工完成、子件入库”保存结果恢复，主入口 static/ui/production-workspace.js 的 group-actions（action=complete；只读比较相邻process/dispose/assemble，不同时改所有流程）。先证据和方案，不实施、不发布。原五环节51检查/21类缺陷已按v593交付，本轮不能重复计数或凑十个Bug。

按 CODEX_START→NAS AI_START→本卡→ORDER_FLOW→总需求7/14/17及执行章程3～9最小读取。根当前订单恢复候选6640d5369e2d48ecba36be9aefae1afa9ed449db / v602正在串行发布；运行正式以state实时证实，不把候选当已发布。所选生产模块应先与该候选及v601正式108de068比对，变更则报告，不在旧树猜新实现。只读审查现有干净分支，禁止checkout/修改被保留隔离服务引用的树。

主目标：实际完成动作在后端已提交但网络回执丢失、坏回执、列表刷新失败或重复点击/刷新/切账号后，是否会误报、遗失原请求、重复加工/消耗/新增产出。验证数量、流水、冻结用料、版本、客户权限、幂等原key及异载荷。已完成后的只读状态不自动等于当前原请求完整证明；not_found不等于取消。首先定位实际页面按钮→方法→端点→事务的精确调用链。

明确允许只读双线协作，无仓库源码写入负责人：
- /root/mobile_drawings_api：actual HTTP、实际事务/commit边界、幂等和数量事实，探针仅写本artifact/api；原chain-order-audit树HEAD9a3a9f33保持，不能改服务文件。先与UI确认真实action和payload，最多必要定向正常/失败/重放/权限集合，不跑全仓。
- /root/mobile_drawings_ui：actual HTML及production-workspace方法，探针仅写本artifact/ui；原chain-production-audit树HEAD0b3189d0保持。测试首写空/截断2xx、真正未知后刷新/关闭/编辑/同key处理、列表失败、切账号迟到；使用实际函数，不能只靠字符串断言或仿造实现。没有视觉改动可先不启动Chrome，不操作正式页面、不用IAB。
- 根：阅读证据，确定真实缺陷或安全现状，写最小修复卡后再实施。

不写正式库存/订单/历史；不运行旧退役数据层；不杀进程或绕过之前自动审批拒绝；不改模型、迁移、版本、代码、测试仓库或Git分支，不push。保留合成测试证据、源指纹、失败/正常结果，简短归类可重现缺陷，未发现亦如实记录。各线写本artifact独立报告与NAS独立回执，状态只读审查完成，修复和正式验收均未完成。
