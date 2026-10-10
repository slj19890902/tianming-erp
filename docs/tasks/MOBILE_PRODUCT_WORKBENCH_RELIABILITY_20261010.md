# MOBILE-PRODUCT-WORKBENCH-RELIABILITY-20261010

2026-10-10 持续优化目标下一最小闭环：只读审查首页/手机共用的产品搜索与资料卡，重点图纸展开、产品/页签切换、地图返回、断网/迟到响应及客户权限。先复现、形成方案，再按既有授权修复有证据的缺陷；本卡初始阶段不修改运行源码。

正式已核验 v0.22.606，源4b66fc4c104cf49bc48b065c20b5aeed17f9403a，包f863ced8beebe2968263f409af2c8beb6a7fe58c6d4bf8c56ff3e4a8b83c912d；根01567c4c仅多发布文档。根分支codex/mobile-product-workbench-reliability-20261010。远端6c206e33为已合并的v605首页工作台发布回执，不回退到此旧指针。发布前必须重新核验。

启动顺序 CODEX_START→NAS AI_START→本卡→PLATFORM→总需求2/3/5/14/15/18/19和章程3～9；只读定位跨模块接口时精确读取对应契约。用户持续优化及普通 validated 发布授权保持，无Git push，无正式数据修正/迁移。此前Chrome启动及旧PID停止审批被拒绝，不重试或换工具绕过；不IAB，不正式自动点击。

本卡明确允许两个只读子任务并行以缩短审查；所有仓库文件由根独占写。双方只读根集成树 D:/.codex/worktrees/factory-reliability-20261009/纸箱厂erp软件搭建；探针、报告仅写自己artifact子目录，不能改工作树或运行正式业务接口。不得碰旧服务或其锁定工作树，合成库沿安全conftest，无新服务器/浏览器。

- API /root/mobile_drawings_api：api目录。精读 app/api/product_workbench.py、app/services/product_workbench.py、相关授权/缩略图调用，实际合成HTTP复现产品/批次身份与客户隔离、停用/缺档、来源和输入边界的高风险项。不要因缺乏覆盖就登记Bug，不重做已有正常检查凑数。复用既有tests/test_product_workbench.py夹具，保存精确源和HTTP证据。只读报告+最小修复建议。
- UI /root/mobile_drawings_ui：ui目录。实际static/mobile_erp.html、product-workbench.js/css、desktop入口精确片段；复用tests/ui内linkedom/VM实际实现，审查缩略图重试、跨产品/账号迟到响应、地图返回、查询/页签在途交互、窄屏相关结构。离线非DOM不是浏览器验收；不可声称屏幕通过。报告真实复现和最小修复建议。
- 根：确认本卡及实际新正式基线，跨接口核验、独立审查、定义后续实施allowlist。正式已有功能不得因修复丢失，保留数量/状态/权限/幂等/版本/事务/审计和历史冻结。

审查不改库存、材料匹配核心、BOM、报料审批、价格或历史资料；需要业务规则变化先独立列出，不夹带。发现问题分类真实缺陷/既有正常门禁/待现场核验。每轮写NAS独立回执，不能只重复状态。持续Goal active。

## 已复现后实施阶段（2026-10-10）

已有实际合成HTTP和真实linkedom/VM审查：API确认已加工子件位置/实际批次反查单位误标、超大int/offset产生500两类；UI确认手机图纸未展开即src、旧show回调污染新产品页签、render丢未提交条件、无限等待四类，空坏2xx另由审查确认后收口。正常地图返回/普通历史切换/销毁后旧结果不渲染不算Bug。修复仅针对实际复现，不扩原数量或匹配规则。

最小方案：API局部按库存实物形态投影原单位，ID与页号/offset边界前置可读4xx；UI把已提交查询和正在编辑条件分离，请求及后续页签恢复均核对同一轮身份，有限等待并拒绝迟到结果，错误/空回执有明确重试，手机复用现有TmProductDrawings实现收起/展开/图片及PDF/失败分类/释放。桌面图纸原呈现保留，手机详情和结果都默认收起。

本阶段明确允许API/UI/独立审者并行，各有写入边界。根核验两旧group-save树干净且无存活python/node/chrome引用后复用，从正式4b66fc4c建立新分支，原分支保留；禁止触碰旧production-save和chain服务引用树。

- API负责人 /root/mobile_drawings_api：D:/.codex/worktrees/group-save-recovery-api-20261010/纸箱厂erp软件搭建，新分支codex/mobile-product-workbench-api-20261010。允许 app/api/product_workbench.py、app/services/product_workbench.py、tests/test_product_workbench.py；artifact/api。先提交失败证据和最小合同，后实现验证，并交最终真实详情/搜索/反查HTTP给UI。禁止改共享数量helper/模型/迁移。
- UI负责人 /root/mobile_drawings_ui：D:/.codex/worktrees/group-save-recovery-ui-20261010/纸箱厂erp软件搭建，新分支codex/mobile-product-workbench-ui-20261010。允许 static/ui/product-workbench.js、static/ui/product-workbench.css、static/index.html、static/mobile_erp.html、tests/ui/product_workbench.test.cjs，可新增 tests/ui/mobile_product_workbench_reliability.test.cjs；artifact/ui。不得改旧TmProductDrawings实现，必要时先报告。只改入口资源版本所需行，不扩大主单体页面业务。复用固定linkedom依赖，不改依赖锁。
- 独立审者 /root/order_recovery_review：只读候选和根，自有artifact/review探针，核当前7类边界、真实API→实际UI及权限/单位守恒、图纸生命周期。未能用真实浏览器时仅声明技术非DOM验证。
- 根独占版本、任务卡、报告、整合与受控发布。目标暂定v607，发布前重新核验正式基线；无迁移/正式历史修正/no push。

必须证据：失败复现→针对性修复回归；实际手机入口展开前0预览请求、展开后按权限加载、收起/切换/销毁取消与释放；旧产品/账号晚回不改当前页签/图纸，未提交输入不被刷新/重试清除；超时退忙可重试且晚响应不能覆盖；坏2xx不报空库存/成功、不崩页；原正常搜索、实际批次反查、地图返回、桌面报料用途与首页相邻不退化。库存数量/版本/客户范围保持，不用快照或窄测试证明全ERP无Bug。发布沿已验证标准Manager签名、冷备恢复、原表/文件保持、唯一head、健康与静态资源门禁，现场验收仍pending。

2026-10-10实施补充：独立审查发现已加工output_piece反查的表单缺对应选项，UI实际重提复现原状态丢失并落后端raw。根授权在原UI文件范围加入“已加工子件”选项，保持原实物身份，独立列第8项，不改变匹配算法/数量/扣库。两线允许文件、数据及发布边界保持。
