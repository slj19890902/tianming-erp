# 货架产品标签大二维码 v0.22.521

状态：正式技术发布及只读验收完成，管理员实体打印/手机验收待完成。2026-09-28。

- 正式代码：55889350380c0d5c48818e7b1e98daf6807bfa1c，基于143ac3983，完整保留v520模具三段式及此前送货单修复。
- 签名包：ec3d66472ec76c6a060801cac1bb26c017066cd925ae85524f29e31c03545c4e；安装D:/TianmingERP，从v519更新至v521，唯一revision ec0927xl，无迁移。
- 已验证NAS完整备份：Z:\sata1-18015598002\BoxERP\backups\20260928-141053-9fc1891d.tmbackup；SHA256 4d6b94701c684d2a610358b261123a4c8f51f864f9b04c13e50c907e02c3bdb2。旧v519签名程序保留，可回退程序而不覆盖正式数据库。

## 修改
产品标签40×80mm纸型、320×640原生位图不变。二维码15×15→30×30mm，右侧x48/y5，横线只到x46；左侧x2..46，2mm间隙。位置来自已发布的人类可读货架名，显示R013 2层 2格，不显示区域前缀与楼层。位置上限20pt、客户14pt、编码16pt、品名/规格11.5pt；文字自适应完整保留，过长拒绝打印，不静默截断。紧凑字段新增，原print_title/print_floor/print_position与整架rack_label不变。

二维码稳定产品key、原URL、M纠错和四模块留白、权限、客户范围、打印资格保持；不修改库存、订单、业务事实。隔离样本URL52字符，版本4/33模块，码点约由2.93提高到5.85像素。不缩短路由、不降低纠错。

## 验证
- 旧实现新增紧凑地址测试KeyError失败，新版tests/test_mobile_shelf_labels.py + tests/test_rack_cell_information_labels.py共11项通过。
- factory_twin/frontend/tests/mobileShelfLabel.test.mjs通过，补齐旧fixture缺失lot/$依赖，覆盖紧凑位置、内容完整、XSS及不打印库存数量。
- 隔离Chrome：三种标签模式、整架身份去重、模式切换、40×80纸型、30mm二维码与左文字边界通过。
- 原生PNG共3张和203dpi PDF共6页，全部实际解码为对应稳定URL。人工查看产品位图无文字与二维码重叠。
- 继承v520模具26项回归通过；版本元数据、唯一head、git diff --check、包签名与Git源码字节核对通过。
- 运行127.0.0.1:18000、192.168.3.80:8000、172.16.1.26:8000健康均200；shelf-label HTML/JS和首页均与签名文件哈希一致；匿名产品标签/查询均401。
- schema不变、完整性和外键检查通过。305表中304表原字段事实完全不变；仅email_intake_settings的last_sync_started_at/last_sync_completed_at/last_sync_received随启动自动收件轮询更新。发布脚本原断言只允许时间字段，因此末尾断言曾报错；已按app/services/email_intake.py:run_automatic_cycle核实第三字段属于运行统计，保留原失败与postdeploy-review.json证据，无正式业务表变化，无数据恢复/回写。

## Claude审核状态
真实只读调用返回“Your account is on hold and can't use Claude Code”，未取得本轮Claude审核结论。Claude之前T7因共享print_title影响整架模式而停止且未保留改动；本次增加独立compact字段解决，不改原共享字段。Codex完成上述复核；不将其写成Claude审核通过。

## 现场人工验收
刷新确认v0.22.521，从货架查货打印产品标签；核对R013 2层 2格、客户/编码/品名/规格及右侧大二维码。按40×80mm、100%缩放打印一张，由管理员用实际手机确认扫码速度和产品对应关系。正式页面未自动点击，未替管理员签发实体验收。

证据：D:/ERP-UAT/shelf-product-qr-20260928；包含product-label.pdf、product-raster.png、chrome-evidence.json、qr-decode-evidence.json、package-check.json、release-facts.json、postdeploy-review.json、health-acceptance.json及Claude失败原始结果。

NAS签名更新源latest.json已指向本版，发布工具已完成全包签名、载荷及NAS复制哈希验证。
