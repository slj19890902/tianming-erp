# VPN-MOBILE-HOST 2026-09-16

现场问题：iPhone 经贝锐蒲公英 VPN 打开 `http://172.16.1.26:8000/mobile/` 返回 `Invalid host header`；同一正式服务的厂内 `http://192.168.3.80:8000/mobile/` 正常。VPN 地址属于本机 OrayBoxVpnEnt，两个地址已有独立 Windows portproxy 转发到正式 ERP 回环端口。

目标：保留公网 HTTPS 与厂内 LAN HTTP 双入口，另将明确配置的蒲公英私网 HTTP 地址接入同一正式 ERP、同一登录与权限。仅信任显式私网地址和端口；Host、来源、Cookie、HTTPS 跳转与 HSTS 门禁一致，跨来源 POST 继续拒绝。不新增第二套 ERP 数据或权限，不修改正式业务事实。

成功条件：172.16.1.26 的手机页及健康接口可打开、HTTP 登录 Cookie 可用；192.168.3.80 和公网入口回归正常；非白名单 Host 仍 400，公网来源伪造/跨入口写请求仍拒绝。隔离测试与正式只读接口、静态资源核验通过后发布，正式手机页面由管理员人工验收。

主上下文 `docs/context/PLATFORM.md`，总需求第 2、3、5、14、15、18、19 章，执行章程第 3、5、7、8、9 节。候选自当前 `origin/factory-current-baseline` v430 建立；正式版本、revision、服务状态须发布前实时复核。单代理、独立 worktree `C:\erp-worktrees\vpn-mobile-host-20260916`。
