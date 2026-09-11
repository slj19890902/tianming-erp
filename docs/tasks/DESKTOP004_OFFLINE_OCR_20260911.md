# DESKTOP-004 离线中文 OCR 模型交付
当前授权完整交付范围内，补齐安装助手离线扫描 PDF 识别依赖。允许 desktop_assistant/build.py、manager.py、新模型打包模块及专属测试。不改变订单识别业务规则和正式数据。模型读取本机已有 EasyOCR 缓存，按随包 EasyOCR 官方配置校验；签名包包含模型，不自动下载。托管启动固定模型路径，不读取旧用户缓存。用真实运行环境、禁止网络下载的识别验证。仍需备用电脑与真实用户扫描 PDF 人工验收。

验证：模型按随包 EasyOCR config 的校验值核实并复制；5 项构建/模型测试通过。实际 Python3.12 离线 runtime 使用独立模型目录、拦截 socket 连接，正确识别合成中文图片及当前 ERP ocr_pdf_bytes 扫描 PDF 中的客户单号123456、数量300。证据 D:/tm-uat/desktop-offline-ocr-20260911/pdf-proof-result.json。不是实际客户订单全字段提取或备用电脑验收。模型未提交 Git，尚未生成含模型的新安装器。
