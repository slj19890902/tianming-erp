# MOBILE-STOCKTAKE-RECOVERY-20261010

持续Goal下一闭环，用户既有全流程可靠性修复/提交/发布授权保持，普通代码发布不重复申请。前一闭环v598已正式/NAS发布：源c010d20facef331f5a3de317d2e839c677b22d6f，包5d4c816aa0f5304c4ff44bf318b736b112508a43f44678f99cb315a53d405758，en1009hp，无迁移；根47项通过，冷备独立恢复/323表及附件保持/健康/静态只读检查通过。文档HEAD bc035ddb，原主目录无关改动保持。Git远端正式指针仍需实时核对，不用落后指针覆盖真实正式祖先；本会话不push。

本卡只修手机盘点提交回执和未知结果恢复。证据：mobile-flow-audit/AUDIT_REPORT.md、11-stocktake-false-ack.png及真实函数探针；API独立mobile-stocktake-recovery-api/probe.xml及commit-then-response-failure.json。空/坏2xx被前端当成功并清键/输入；真实confirm在db.commit后构造返回payload，如发生SQLite busy，API返回409 STOCKTAKE_DRIFT但approved/审核/库存流水已存在。同原key/完整原body重放201且零新增。不得把首次409一律当未写，也不得声称正式历史已出现此异常。

主模块WAREHOUSE，按CODEX_START→NAS AI_START→本卡→WAREHOUSE及总需求9.1/15/17、执行章程3至9阅读；已读未变上下文复用。API现行StocktakeCreateRequest不识别expected_actor_id；confirm存储键为mobile-confirm-加原键SHA256，普通上报存原键。现存精确重放允许不同有权限管理员读取原操作者的回执；当前认证对新写入仍权威，不把该事实误称越权。保留已有角色/权限/全部客户范围要求及盘点差额、短缺预占释放、版本/位置/事务/审计合同。

方案：
1. 后端confirm及员工submit在事务提交前构造完整业务回执，减少已提交后被错误分支伪装拒绝；提交本身未知仍须由UI保留请求。保留完整回滚和精确幂等重放；不得更改现有库存算法或批量重填历史。
2. 增加专用只读原提交核对入口，使用原动作（confirm/submit）、原key及完整原body按现存持久StocktakeOrder/Review匹配，校验当前认证/该动作现行权限和客户范围。返回明确已找到的真实回执或not_found；not_found只代表当前未找到，绝不证明原请求永不执行。不得把查询实现成create/approve，不伪造流水或新注销表。
3. 请求增加可选expected_actor_id并在写前校验真实当前认证，兼容旧客户端省略；它仅是切账号防护，不是历史归属证据。核对回执明确当前认证和历史submitted_by，跨账号不自动接管本地待确认请求。提交/resolve响应结构及状态合同先由API执行者发给UI执行者及根确认一致，再写依赖代码；不用当前take的字段假定盘点已有。
4. UI发送前持久保存原动作/key/原完整body/当前账号和原输入，存储失败阻止发送。核对完整盘点id、单号、货位、签名字段、实际状态、身份和命令键后才认定成功。空/坏/部分2xx、网络错误及任何不能证明原请求已处理的结果保留同一记录/输入，冻结改数量或换键，提供清楚“核对这笔盘点”入口。重载后可恢复同一请求；明确继续重试只发原key/原body。
5. 已确认成功与后续库存刷新分开；刷新失败只重试读取。此前unknown后的409/403/401不能删原记录；切账号及晚回响应不污染新账号/新页面。已确认成功但清缓存失败保持confirmed，避免重复保存。普通员工显示实际上报/审核状态，管理员approved才显示库存更新；后续审核状态以真实回执显示。

根实施复核补充：每个原请求独立持久键，按账号列所有待核对项；未知或confirmed清理失败只限制原货位的新建/改数，不能卡住该账号其他货位。切换货位仅切视图，不改/删原记录；其它位置仍按最新读取及原后端门禁执行。至少验证共享storage两个页面互不覆盖、两货位操作不串记录。不用localStorage读写冒称原子锁。首次版本变化且resolve未找到时也不能宣传“再试即可”，需准确展示原事实/拒绝原因及管理员核对入口；安全终止仍是后续持久合同，不私加删除键。

不实现未知取用注销/tombstone，不处理盘点跨页查货条件恢复或触控布局（已登记后续P2），不重构通用api或整套手机框架，不改正式数据、权限、迁移、报价/成本、旧系统。若完整恢复需额外持久业务表或修改旧reader合同，先给根精确证据再扩大范围，不能偷偷迁移。

分工：最多两执行线，独立worktree且每文件一个写负责人。
- API /root/mobile_drawings_api：从本卡SHA新建codex/mobile-stocktake-recovery-api-20261010，独占app/api/stocktake.py、必要的app/services/stocktake.py、tests/test_mobile_stocktake_recovery_api.py；其他服务只读，先失败真实HTTP回归。任何要改共有库存函数先报告。输出接口契约、候选提交、NAS回执。
- UI /root/mobile_drawings_ui：从本卡SHA新建codex/mobile-stocktake-recovery-ui-20261010，独占static/mobile_stocktake.html、必要独立static/ui/mobile-stocktake-recovery.js、新tests/mobile_stocktake_recovery.cjs及必要专用Python包装；API定义只读。先复现现有函数失败，等API契约固定再实现依赖。现有取用/图纸/添加入口不改。
- 根独占卡、版本/汇总、独立复核、合并和串行签名发布。原主目录与其他候选不动。

根复核补充测试维护：本次专用恢复组件取代旧submit函数内联写法后，tests/test_n035_stocktake_frontend.py新增两条源码形态断言失败。根独占该文件，入口契约检查HTML实际引用的module、保留禁止直接adjust；提交busy及恢复禁改用完整实际HTML+module行为回归验证，不把无效字符串加回产品代码。原三个基线旧失败单列，不扩改无关测试。

最短回归：真实confirm/employee submit、已完成精确重放/异载荷、位置或库存后来变化仍可核对原结果、未找到不写、当前权限/客户限制/账号切换、commit前返回构造失败全回滚/commit后或传输未知可核对、库存调整/预占/审核守恒；前端坏2xx、正确全回执、断网/双击、unknown后重载/编辑、所有错误分支、成功后刷新失败、存储异常和晚响应。原盘点关键回归只选受影响最短组；无全仓、单组小于10分钟。必要UI用隔离Chrome，禁止IAB/正式自动点击/Playwright，截图不能代替真实后端或管理员手机验收。

完成后根核对候选/正式实时基线、唯一head、签名、冷备独立恢复、业务/附件保持、完整性/FK、健康及本次只读资源；普通代码正式技术交付后给1～3条最短人工入口，未收到反馈不写人工通过。NAS独立回执；持续Goal active。
