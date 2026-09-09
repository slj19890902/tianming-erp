# 手机摄像头连续扫码

范围：用户反馈 ERP 没有摄像头授权操作；补齐仓库可见入口与用户点击开启相机，在同页连续扫描现有货位/货位产品码并调用现有库存读取与盘点入口。

基线：origin/factory-current-baseline e85962aa；独立分支 codex/mobile-camera-scan-20260909。
根因：没有 getUserMedia/二维码识别实现，且全站 Permissions-Policy camera=()。

不变量：相机帧仅本地处理；不改库存、订单、账号、客户范围或审核；二维码不授权，既有 API 后端门禁保留。只对 /mobile/scan 允许 camera=(self)，其他页仍禁用，麦克风和定位不开放。相机仅用户点击启动；取消、后台、离页及成功识别停止轨道，异步授权晚返回仍释放。

不做：路由器/DNS/证书调整、内外网自动切换、微信 JS SDK、未经验证的 iPhone 实机验收承诺。

实现：复用轻量库存页及登录，摄像头入口内联控制脚本；本地 jsQR 1.4.0 首次点击按需加载，避免 CDN。仅接受已知 ERP 主机的货位码并提取 ID，不导航二维码地址。HTTP 内网给出 HTTPS 链接。

验证：4 项 Python 入口/响应头隔离；5 项 Node 摄像头/连续扫描/取消晚授权/拒绝/HTTP；4 项原库存卡/盘点入口回归；真实编码 QR 像素用本地 jsQR 解码成功。唯一 migration head rs08v8x9z67，无新迁移。实际 iPhone Safari 许可、后置相机对焦及两货位连续读取待人工验收。
