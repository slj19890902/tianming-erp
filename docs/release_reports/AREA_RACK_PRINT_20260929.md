# AREA_RACK_PRINT_20260929

需求：区域直接批量打印整架编号+手机二维码；调查缺码、重叠和机身FEED半张。
代码：45f261501e1fa2d20ecf3666700649501de59e44，分支codex/rack-batch-print-20260929，候选v0.22.531。

## 已验证
- npm run build：TypeScript与Vite通过；旧大块资源提示未改变。
- node --test factory_twin/frontend/tests/rackLabelBatch.test.mjs：3通过，按稳定货架ID分组、同名分架、空间排序、错楼层/非活动/无正式货位/超500架阻止。
- pytest tests/test_mobile_shelf_labels.py tests/test_rack_cell_information_labels.py -q：11通过，测试令牌短密钥既有警告2项。
- 隔离Chrome：区域URL直接rack模式、两架两张、完整320x640栅格、坏QR阻止打印、每层每格/产品标签相邻回归；未点击正式页面。
- OpenCV解码三种打印栅格成功，保持原手机访问身份。
- alembic heads唯一ed0928ml；git diff --check通过；无迁移。
- 未跑全量：范围限于标签前端及已有API的授权回归。

## 故障边界
用户确认机身FEED走半张，证明独立于ERP渲染的走纸异常存在；尚未确定纸长状态与传感器校准中哪个是最终根因。本机三个队列均USB002；两个40x80队列的PrintTicket为40000×80000微米，旧Gprinter队列高度30000微米，存在发送错误纸长风险，但不能据此宣称已证实最后使用了旧队列。保留全部打印机配置，无硬件重置或实机试印。QR、文字已在一个栅格中，新增加载完整性守卫不代替硬件校准。

## 人工验收
1. 打印机选择Gprinter GP-3120TU - 40x80纵向标签，宽40高80mm，间距感测/真实间隙，驱动测试页校正；连续FEED每次完整一张。
2. 仓库地图点击区域→打印此区域货架编号＋二维码；每架一张、无需切换内容。
3. 试打两张核对不重叠、完整二维码、手机扫码定位正确。
官方参考：https://www.gprinter.net/rzyxl/352.html 。不把自检或复位按键冒充通用校准。
实际token用量不可获取，不估算。

## 正式发布结果

技术发布完成，待管理员人工验收。包 03a6c9706b3a64425cccba3a81f5452cb05edd7992cfad4fcd93dd1291e5eb72。NAS签名更新源已同步；运行代码45f26150，唯一head ed0928ml，无迁移。
备份 Z:\sata1-18015598002\BoxERP\backups\20260929-132620-64369bf6.tmbackup，sha256 92eff1cfbc86241cfa9cc64f83624935e39de6de9e0e426fa986e3a2aef6317a，已验证可读与恢复内容。305表冷态事实不变；启动后仅邮件同步运行状态/时间变化；完整性ok、外键0，送货打印校准文件未变。三地址health与5项页面/资源SHA核对通过。
回滚点：前包 d2ec51c374481e94ec6e69d93079ca89f2ee24c938ffbeba1c491df418ee3292 与上述已验证备份，采用受管更新器恢复；无数据库迁移，不覆盖业务库。
