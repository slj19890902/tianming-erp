# 天明纸箱厂 ERP

轻量级纸箱厂生产管理系统，基于 FastAPI + SQLite，支持订单、送货、采购、财务等核心业务流程。

## 目录结构

```text
├─ main.py              # FastAPI 入口（启动时自动建表）
├─ requirements.txt     # 后端依赖
├─ app/
│  ├─ api/              # 路由层：auth、orders、deliveries、finance 等
│  ├─ models/           # SQLAlchemy ORM 模型
│  └─ services/         # 业务逻辑（PDF、Excel、对账等）
├─ alembic/             # 数据库迁移脚本
├─ tests/               # pytest 测试套件（按阶段组织）
├─ scripts/             # 数据导入 / 维护脚本
├─ docs/                # 设计文档与迁移计划
├─ tm_frontend/         # Vue 3 前端（Vite）
├─ static/              # 打包后的前端静态文件
└─ deployment/          # 部署配置与启动脚本
```

## 本地运行

```powershell
pip install -r requirements.txt
python main.py
```

启动后终端显示局域网访问地址和二维码。

## 默认账号

| 角色 | 用户名 | 密码 |
|------|--------|------|
| 老板 | boss | 123456 |
| 车间 | workshop | 123456 |

## 打包（Windows EXE）

```powershell
pyinstaller --onefile --name carton-erp --add-data "static;static" main.py
```

数据库自动创建在 exe 同级的 `data/carton_erp.sqlite3`。

## 运行测试

```powershell
pip install -r requirements-dev.txt
pytest tests/
```
