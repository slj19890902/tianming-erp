# 三级纸箱厂极简 ERP

## 当前目录结构

```text
纸箱厂erp软件搭建/
├─ main.py              # FastAPI 入口；启动时自动创建 SQLite 表
├─ requirements.txt     # 后端依赖
├─ README.md
└─ data/                # 首次运行后自动创建，存放 SQLite 数据库
```

## 本地运行

```powershell
pip install -r requirements.txt
python main.py
```

启动后终端会显示局域网访问地址和二维码。

## 账号初始化

系统账号固定为 `admin`、`finance`、`sales`、`workshop`，不提供可用于生产的默认口令。

- 开发/测试环境首次初始化会创建测试账号，并标记为首次登录必须改密。
- 生产环境默认禁止账号引导。只有 `users` 表为空、显式开启
  `ERP_ALLOW_PRODUCTION_USER_BOOTSTRAP=1`，并通过进程环境提供符合当前密码策略的
  `ERP_INITIAL_PASSWORD` 时，`init_db.py` 才会创建四个账号。
- 生产库一旦已有账号，`init_db.py` 会拒绝新增或修改默认账号；后续账号维护必须使用
  受控管理脚本，不能靠重新初始化覆盖。
- 初始化口令不得写入仓库、命令行参数、日志或结果文件。正式换密使用交互式
  `scripts/admin/final_password_handoff.py`。

## PyInstaller 打包

Windows 下需要把 `static` 文件夹一起打进 exe：

```powershell
pyinstaller --onefile --name carton-erp --add-data "static;static" main.py
```

打包后的数据库会自动创建在 exe 同级的 `data/carton_erp.sqlite3`。

## 后续规划目录

```text
backend/
├─ api/                 # 登录、订单、送货、财务等 API
├─ services/            # PDF、Excel、对账、利润计算
├─ repositories/        # SQLite 数据访问层
└─ security/            # 登录、RBAC、密码哈希

frontend/
├─ index.html
├─ assets/
└─ src/                 # Vue 页面和组件

storage/
├─ uploads/             # 图纸、刀模图、回单照片
├─ pdf/                 # 报价单、报材料单、送货单
└─ exports/             # 对账单和毛利表 Excel
```

当前先保持 `main.py` 单入口，方便 PyInstaller 打包；业务代码变多后再按上面的结构拆分。
