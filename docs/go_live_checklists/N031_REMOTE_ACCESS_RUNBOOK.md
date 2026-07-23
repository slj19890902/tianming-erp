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
