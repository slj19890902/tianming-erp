# 货架标签位置分隔 v0.22.528

状态：技术发布完成，待管理员纸面验收。

- 用户要求货架号、层数、格数用-分开。商品标签预览与Canvas实打共用productAddress，显示R013-2层-2格；兼容旧接口空格位置文本。普通货位两行地址的货架后加-，每格商品信息标签显示架-02层-03格。
- 后端print_address不再将层-格改为空格。整架编号+二维码模式、二维码URL、纸型/尺寸、打印位图方式、权限及实物位置不变。无迁移，不写正式业务数据。
- 11项定向测试通过：test_mobile_shelf_labels.py、test_rack_cell_information_labels.py。既有地图产品打印入口断言已随实际encodeURIComponent(lotId)更新，原断言过时，不改地图业务代码。
- 隔离Chrome验证3个独立货位、整架模式分组与切换、40×80尺寸、文字/QR边界、商品标题及Canvas实际绘制文字为R013-2层-2格。已目检生成位图。未运行全仓测试，未自动点击正式页面。
- 来源873fd22b50e017a2078477ad8321aab7e6e7c25b，基线v527/73f9bc57，唯一head ed0928ml。原工作区无关改动保留。
- 证据D:/tm-uat/rack-label-separators-20260928，含chrome-evidence.json、product-raster.png。

管理员操作：刷新ERP→仓库地图→打印货架标签（或货位产品标签），重开预览并先打印一张，核对货架号-层数-格数。整架模式仍不出现层格。

真实token用量无法获取，未估算。

## 正式核验
- 三网健康200/ok，首页和本次标签HTML/JS均与签名包哈希一致。
- 包：dfcccb6c8f7e3fa909fd3faa55f3f9a8545f1e72a00cdfec3dccd50e43b3f1ba
- 已验证备份：Z:\sata1-18015598002\BoxERP\backups\20260928-201858-cfe6fa7a.tmbackup
- 备份SHA256：e3e6927090dde41fbc86dfe79d215ec518cc3670562a1a6d5b4018c9fab541df
- 305表停服事实不变；启动后仅邮件轮询字段自然变化，完整性ok、外键0，无迁移，业务数据未修改。回滚点v527。
