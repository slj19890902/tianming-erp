# MOBILE-ALL-DRAWINGS-20261010

## 授权与范围
老板要求：手机查看常用箱图纸，多次上传的全部展示，不能仅显示最近一张。沿用普通代码发布长期授权。

复用已干净、与正式 v0.22.610 同基线的独立 worktree，新建分支 codex/mobile-all-drawings-20261010。初始 SHA f99bb014835e6ac40b1834ddcadb7aad2745c25a；发布前重新核对。

## 最小闭环
检查上传留存 → 手机产品图纸元数据返回全部工程附件 → 缩略图列表逐张读取/放大 → 定向回归 → 备份恢复校验 → 正式代码发布、只读核对、NAS 回执。

## 文件负责人和边界
主代理：app/services/mobile_product_drawings.py、tests/test_mobile_product_drawings.py、相关入口、需求/任务/回执、版本及发布。
前端子任务：static/ui/mobile-product-drawings.js、static/ui/mobile-product-drawings.css、tests/mobile_product_drawings_ui.cjs。只读审核子任务无文件写权限。本卡批准上述小范围并行。
按证据确需扩展入口时主代理记录。禁止修改正式业务数据、文件上传历史、库存、成本、客户范围、生产任务冻结图纸和权限。

## 验收
同产品多个工程附件全部可查、可分别查看；顺序稳定；缺失/损坏单图不阻断其他图；无图清楚显示。印刷素材不混入，跨客户无权读取，不以最新主档覆盖冻结任务。仅运行相关测试，不全量回归；正式页面由管理员手动查看。
