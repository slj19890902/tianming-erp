# N031 远程访问安全部署手册

## 1. 推荐方式

优先使用 Tailscale 或 WireGuard VPN；只有确有外网访问需要时，才使用公网 HTTPS。
两种方式都必须保持以下边界：

```text
用户 -> VPN/HTTPS -> Caddy 或 NGINX -> 127.0.0.1:8000 -> 天明 ERP
```

- ERP 后端只监听 `127.0.0.1`，不得直接开放 8000 端口。
- 反向代理是唯一入口，只开放必需的 VPN 端口或 HTTPS 443。
- 正式账号不得使用隔离 UAT 的默认测试密码。

## 2. 生产环境配置

以下均为占位值，不要把真实密钥提交到 Git：

```ini
ERP_ENVIRONMENT=production
ERP_PRODUCTION_TRANSPORT=https_proxy
ERP_BIND_HOST=127.0.0.1
ERP_PORT=8000
ERP_WORKERS=1
ERP_ALLOWED_ORIGINS=https://erp.example.com
ERP_TRUSTED_HOSTS=erp.example.com
ERP_TRUSTED_PROXY_IPS=127.0.0.1
ERP_HEALTH_URL=https://erp.example.com/api/health
ERP_BROWSER_URL=https://erp.example.com/
ERP_SECRET_KEY_FILE=D:\secure\erp-session-secret.key
```

密钥至少 32 个字符。生产环境的来源必须是显式 HTTPS 地址，代理信任范围只能是实际的本机代理地址。

## 3. 工厂本地局域网直连例外

没有跨网直接访问需求、ERP 仅供受控工厂私网电脑使用时，可以显式配置 `ERP_PRODUCTION_TRANSPORT=lan_http`。该例外不是远程访问方案，不允许路由器端口映射或公网暴露；TCP 8000 必须由 Windows 防火墙限定到工厂 Private 网段。具体配置、启动和发布门禁见 `docs/P0A_STARTUP_RELEASE_RUNBOOK.md`。

该模式禁止 `ERP_TRUSTED_PROXY_IPS`，不启用 HTTPS 跳转/HSTS，Cookie 因 HTTP 不能使用 `Secure`；可信 Host、精确 Origin/CORS、写请求 CSRF、`HttpOnly`、`SameSite=Lax` 和其他安全响应头仍保持启用。需要跨网络直接使用 ERP 时，必须改回本手册推荐的 VPN/HTTPS 架构。

## 4. 代理头与 Cookie 约束

代理必须覆盖而不是追加：

- `Host`
- `X-Forwarded-Proto`
- `X-Forwarded-For`
- `X-Real-IP`

生产 Cookie 使用 `Secure + HttpOnly + SameSite=Lax`。带 ERP 会话 Cookie 的写请求还必须携带与 `ERP_ALLOWED_ORIGINS` 一致的 `Origin`。浏览器会自动完成；命令行或交接脚本必须主动添加同源 `Origin`。

## 5. 上线步骤

1. 备份正式 SQLite 数据库，并记录当前 Alembic 版本。
2. 校验 Caddy/NGINX 配置，不要先开放公网端口。
3. 启动 ERP，确认本机 `http://127.0.0.1:8000/api/health` 返回 `{"ok":true}`。
4. 启动代理，确认外部 `https://erp.example.com/api/health` 正常。
5. 确认 8000 只能本机访问，HTTP 会跳转 HTTPS，非法 Host 返回 400。
6. 登录并检查 Cookie 含 `Secure`、`HttpOnly`、`SameSite=Lax`。
7. 检查响应头含 HSTS、`nosniff`、防嵌入和 Referrer Policy。
8. 验证退出后旧会话失效，连续错误登录会被限流。
9. 确认证书自动续期及代理日志目录可写。

## 6. 回滚

1. 保存故障时间和日志，停止反向代理入口，不删除数据库。
2. 恢复上一份已验证的代理配置并重新校验。
3. 检查本机与外部健康接口。
4. 若是应用问题，切回上一已验收版本并重启。
5. 若涉及迁移，先停服务并再次备份，再按已批准的迁移降级流程操作。

数据库恢复或迁移降级必须单独确认，禁止直接覆盖正式库。

## 7. 配置示例

- Caddy：`docs/go_live_checklists/Caddyfile.n031.example`
- NGINX：`docs/go_live_checklists/nginx.n031.example.conf`

示例域名和证书路径必须替换，且不得写入真实密码或会话密钥。

## 8. 工厂免费接入实例（2026-09-09）

- 老板限定免费、无自有域名、手机浏览器直接访问。当前入口为 `https://tianmingerp0909.share.zrok.io/`，由老板持有的 zrok 免费账号保留；免费额度与可用性以服务商实时规则为准。
- 手机直达 `https://tianmingerp0909.share.zrok.io/mobile/`。代理把 Android/iPhone/iPad/iPod/Mobile 的无参数 `/` 或 `/index.html` 临时跳转到 `/mobile/`；电脑首页不跳转。带 `redirect` 的登录回跳和其他有参数入口保留原路径，防止未登录循环；同源原账号及后端权限继续生效。首页返回 `Vary: User-Agent`，移动跳转不缓存。
- 请求路径：手机 HTTPS → zrok → 工厂本机 Caddy `127.0.0.1:18180` → ERP `127.0.0.1:8000`。TLS 在服务商入口终止；工厂侧穿透连接由 zrok 加密，Caddy 的 HTTP 监听只在 loopback。没有开放路由器公网 8000 端口。
- `.env` 使用 `production / https_proxy`、精确 HTTPS 来源和可信域名、loopback 可信代理、Secure Cookie；数据库仍为本工厂原库。原局域网 HTTP 直连已关闭，电脑和手机使用上述 HTTPS 入口。外部通道或工厂宽带不可用时，该入口不能使用；旧二维码中的私网地址不会自动变成外网可达地址。
- 部署目录为 `D:\tm-remote-access`，仅管理员和 SYSTEM 可访问；包含 Caddy 配置、已核验官方发布哈希的 zrok 1.1.11/Caddy 2.11.4、切换脚本和受限回滚目录。实际任务使用原生 EXE，不依赖 PowerShell 执行策略。
- 已部署的非敏感代理配置存档见 `caddy.n031.factory.example.conf`。生效文件是 `D:\tm-remote-access\Caddyfile`，ERP 恢复副本为 `Caddyfile.erp`；修改后先用 Caddy `validate`，再向 loopback `127.0.0.1:20190` 执行 `reload`，无需为入口跳转重启 ERP。
- Windows 任务 `Tianming ERP Free Tunnel`、`Tianming ERP HTTPS Proxy` 以 SYSTEM 开机运行，配置退出失败后重试。zrok 使用 SYSTEM profile 下的 `.zrok` 设备身份；不得提交、打印或上传身份文件、授权码与会话密钥到 Git/知识库。
- 开机/恢复后检查两个任务运行、三个端口 8000/18180/20190 仅 loopback、外网 `/api/health`、未登录 API 拒绝，以及手机真实登录。Caddy 的 20190 管理接口只用于本机配置重载。
- 当前 `.env` 必须与代理配置一起保留。仅恢复 ERP 代码和数据库不能自动恢复 Windows 任务与 zrok 设备身份；设备更换后需由账号持有人重新授权连接，随后重新验证固定地址和手机访问。不要恢复旧例子的 LAN 配置后仍保留公网转发。
- 回滚仅恢复原运行配置并将代理切回独立测试页，使用标准发布库停启 ERP；不覆盖或恢复数据库。时点备份、结果、当前人工验收状态见 NAS 独立回执 `20260909-手机5G免费HTTPS接入正式切换.md`。
