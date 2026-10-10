# 产品查询与手机图纸可靠性：整合验证

2026-10-10，MOBILE-PRODUCT-WORKBENCH-RELIABILITY-20261010。目标v607，承接正式v606源4b66fc4c，无迁移，head en1009hp。此记录为发布前技术验证；最终上线状态另附正式发布回执。

范围为9类实证根因，见MOBILE_PRODUCT_WORKBENCH_RELIABILITY_PLAN_20261010.md。API候选37361f51e5d5fa11809547bfe230f166b14a4e19；UI最终57d267de718854afdcea17348205a1c3cc6bbc76（替代d75bd05f）。根621b38ca整合，9个负责人源文件与最终候选LF一致。根独占版本及文档，其余运行差异仅7文件：2后端查询文件、2入口引用、查询JS/CSS及版本。没有共享数量算法、权限、模型或迁移改动。

根合成API 14项通过/33.06s；独立后端2项通过/7.269s，含SQL写监听0、片/张单位、同库存实存/预占/version保持、显式跨客户绑定及原owner隐藏、完整分页offset与ID边界。API最初两项真正修复期望为红，最终绿；不把旧观测成功当修复证据。

根最终UI 56项通过（新29、旧产品11、首页16），原图纸生命周期脚本通过。最初6项、加工子件重提、两类核心坏包以及最终重试/空组两边界均有真实失败记录。d75曾通过54但独立复核检出2边界，未构建/发布；后续57d补齐并根再次56通过。坏包同根变体不增加Bug数。

独立最终5个区块通过：真实已加工片详情→实际用途表单重提；合法hidden/null/inactive；损坏必要包络和空库存组拒绝；切模式后本地失败重试无旧产品请求；原手机图纸组件展开前0fetch、切产品/账号/收起取消释放，虚拟超时15秒及旧history续调不覆盖当前产品。作者14份真实响应解码归档与嵌入UI逐字段/文件SHA等价；独立另外记录15条真实response.text与body一致性。不得将解码归档称为保存过HTTP传输原始字节。

两HTML引用均为JS LF SHA前缀4f07fc902f70、CSS1bbc57da356b；根验证器再次计算实际文件与引用，签名部署后核对正式URL字节。合法无库存、权限隐藏与读取失败分别显示，不将缺档或坏回包当零库存。实际数量、来源、配比、匹配、权限、事务、幂等与历史事实保持。

测试全部合成库或离线实际实现，不写正式业务数据、不启动新服务/Chrome、不终止旧PID，不使用IAB。API既有测试短JWT警告仅合成夹具。真实浏览器网络/Cookie、布局触控、真实PDF像素、打印和管理员现场验收仍pending；之前审批拒绝未重试绕过。

发布门禁：精确正式/远端CAS、签名、Manager新冷备独立恢复、重启前全部原表/共享文件保持、唯一head、完整性/FK、本机及LAN健康、4个相关静态资源与2个匿名只读拒绝。NAS发布源单独CAS核验。没有Git push和正式历史修正。

证据目录D:/.codex/visualizations/2026/10/10/product-workbench-reliability-release-v607：root-api.xml、root-ui.json/tap、root-drawings.txt、candidate.json、validation.json及后续发布/备份/静态检查。原红绿和独立证据在相邻mobile-product-workbench-reliability下；报告已存NAS独立任务回执。

v607技术发布完成，待管理员人工验收。源ef952a8d60d60683b167d2890c471d37a686aa42，包e576e9d0eb176ef619938e3ec603871b35476e0785eb6faa4d12cee18ddfb5c5；正式/NAS一致。备份恢复、原业务/附件保持、健康/资源通过。详见FACTORY_RELIABILITY_RELEASE_V607_20261010.md。
