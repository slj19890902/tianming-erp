# VPN-MOBILE-HOST v0.22.431 发布回执

2026-09-16 技术发布完成，待管理员用 iPhone 人工验收。

现场 `http://172.16.1.26:8000/mobile/` 的 `Invalid host header` 来自正式 Host 白名单只包含公网域名及原厂内入口。v431 新增最多四个显式私网 HTTP 入口配置；本机仅配置 `http://172.16.1.26:8000`。该地址与原 `192.168.3.80:8000`、公网 HTTPS 共用正式 ERP、登录、权限和数据。Cookie、Host、来源、HTTPS 跳转及 HSTS 使用同一明确入口集合；陌生 Host 和跨入口写请求仍被拒绝。原有两个 Windows portproxy 均保留。

隔离双入口与传输安全回归 52 项通过，`git diff --check` 通过。唯一代码/正式数据库 migration head `cc0915`，本版无新迁移；正式库只读完整性 `ok`、外键问题 0。三个手机入口及各自健康接口均 200，蒲公英未登录 `/api/auth/me` 为 401、陌生 Host 为 400；蒲公英手机 HTML 与签名包源码 SHA256 一致。没有正式浏览器自动点击或实际手机登录验收。

公网 zrok 曾返回 502：隧道进程仍在，但 Caddy 旧配置还监听两个已由 portproxy 占用的 `8000` 地址，定时任务启动失败。已保存原配置到 `D:\tm-remote-access\private\Caddyfile.before-v431-20260916`，将 Caddy 限为公网隧道回环端口 `127.0.0.1:18180`，验证配置后恢复其定时任务；公网健康与手机页均重新返回 200。没有改动内网或蒲公英 portproxy 规则。

- 运行源码 SHA：`68ca145803edd80c03d6444d09e0cf7b926f1bac`；候选 `codex/vpn-mobile-host-20260916`，正式 `factory-current-baseline` 已快进并推送。
- v431 签名包 SHA256：`e5e88ba1518d6eb7f967df1b45276ae28e8af31831e06de6c5461374e1582551`；前版 v430 包：`88e5ab817e7177a567755a957f4e6ccef2fe67eaa128aaf8ce3060bb2bc9c6af`。
- 托管更新完整备份：`Z:\sata1-18015598002\BoxERP\backups\20260916-092322-ee006bae.tmbackup`，解密验证与 NAS 文件 SHA256 复核通过：`cc836cc3abd0d4b47df66215c67eeebbe01752560460bcfdc027b75923f52bfe`。
- 更新后正式配置只加 `ERP_ADDITIONAL_PRIVATE_HTTP_ORIGINS=http://172.16.1.26:8000`；配置变更前先有上述验证过的备份。正式订单、库存、客户范围、账号和权限事实未被修改。

管理员验收：iPhone 连接蒲公英后打开 `http://172.16.1.26:8000/mobile/`，用原账号登录并打开仓库；再核对厂内电脑原地址能正常查看。手机登录与实际盘点未由自动测试代填验收通过。
