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

## 默认登录

```text
老板端：boss / 123456
车间端：workshop / 123456
```

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
