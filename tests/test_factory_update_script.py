from __future__ import annotations

import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = (
    PROJECT_ROOT / "scripts" / "admin" / "update_erp.ps1"
).read_text(encoding="utf-8")
RELEASE_SCRIPT = (
    PROJECT_ROOT / "scripts" / "admin" / "release_erp.ps1"
).read_text(encoding="utf-8")
RELEASE_HELPER = (
    PROJECT_ROOT / "scripts" / "admin" / "release_erp.py"
).read_text(encoding="utf-8")


def test_legacy_update_entry_is_fail_closed() -> None:
    assert "旧的一键更新入口已停用" in UPDATE_SCRIPT
    assert "release_erp.ps1" in UPDATE_SCRIPT
    assert "alembic" not in UPDATE_SCRIPT.lower()
    assert "git fetch" not in UPDATE_SCRIPT
    assert "git merge" not in UPDATE_SCRIPT


def test_factory_update_reports_current_release_version() -> None:
    from app.version import (
        APP_BUILD_DATE,
        APP_CHANGES,
        APP_CHANGELOG,
        APP_EXTERNAL_ACCEPTANCE_REQUIRED,
        APP_VERIFICATION_STEPS,
        APP_VERSION,
        APP_VERSION_NAME,
        current_release_metadata,
    )

    assert APP_VERSION == "v0.22.163"
    assert APP_VERSION_NAME == "真实FIN自动入库与既有地堆发布"
    assert APP_BUILD_DATE == "2026-08-22"
    assert APP_EXTERNAL_ACCEPTANCE_REQUIRED is True
    metadata = current_release_metadata(expected_version=APP_VERSION)
    assert metadata["external_acceptance_required"] is True
    assert metadata["changes"] == APP_CHANGES
    assert metadata["verification_steps"] == APP_VERIFICATION_STEPS
    assert 1 <= len(metadata["changes"]) <= 5
    assert 1 <= len(metadata["verification_steps"]) <= 5
    current_release = [
        item for item in APP_CHANGELOG if item.startswith(f"{APP_VERSION}：")
    ]
    assert any("本次更新｜" in item and "FIN-001～003" in item and "真实空位置" in item for item in current_release)
    assert any("本次更新｜" in item and "原位认领" in item and "不重复创建" in item for item in current_release)
    assert any("本次更新｜" in item and "送货清空" in item and "空间占用" in item for item in current_release)
    assert any("本次更新｜" in item and "F1-DISPATCH-01" in item and "兼容" in item for item in current_release)
    assert any("如何验证｜" in item and "FIN-001" in item and "原 10 个真实位置" in item for item in current_release)
    assert any("如何验证｜" in item and "待来料" in item and "同一位置" in item for item in current_release)
    assert any("如何验证｜" in item and "旧 F1-DISPATCH-01" in item for item in current_release)
    assert any("如何验证｜" in item and "v0.22.163" in item and "de39v8x9z28" in item for item in current_release)
    assert any(item.startswith("v0.22.162：本次更新｜") and "正常实收" in item and "报料材质" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.162：如何验证｜") and "de39v8x9z28" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.160：本次更新｜") and "订单生产用途" in item and "片料备库" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.160：如何验证｜") and "cc37v8x9z26" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.159：本次更新｜") and "误判为没有变化" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.159：本次更新｜") and "结构化规格" in item and "版本预检" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.159：本次更新｜") and "不自动修改Z.004.000006" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.159：如何验证｜") and "Z.004.000006" in item and "常用箱已更新" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.159：如何验证｜") and "原样回读" in item and "蜂窝板厚度" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.159：如何验证｜") and "bbb36v8x9z25" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.158：本次更新｜") and "静默还原" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.158：本次更新｜") and "不能覆盖常用箱" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.158：本次更新｜") and "真正删除" in item and "永久审计" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.158：本次更新｜") and "bbb36v8x9z25" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.158：如何验证｜") and "Z.004.000006" in item and "原样回读" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.158：如何验证｜") and "只读显示" in item and "没有可编辑比例输入" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.158：如何验证｜") and "撤回到未报料" in item and "审计" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.158：如何验证｜") and "bbb36v8x9z25" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.156：本次更新｜") and "材质" in item and "孔径" in item and "长×宽×厚" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.156：本次更新｜") and "删除订单组" in item and "409" in item and "500" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.156：如何验证｜") and "Z.004.000006" in item and "默认采购比例" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.156：如何验证｜") and "aaa35v8x9z24" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.155：本次更新｜") and "订单1→采购1" in item and "订单1→采购2" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.155：如何验证｜") and "aaa35v8x9z24" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.154：本次更新｜") and "纯外购" in item and "蜂窝板" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.149：本次更新｜") and "不同楼层" in item and "当前楼层" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.149：如何验证｜") and "v0.22.149" in item and "yy33v8x9z22" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.148：本次更新｜") and "多个正式客户" in item and "最多两个主标签客户" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.148：本次更新｜") and "模具标签名称" in item and "内部模具编号" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.148：本次更新｜") and "yy33v8x9z22" in item and "不按名称猜测" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.148：如何验证｜") and "双主客户" in item and "/" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.148：如何验证｜") and "40×30" in item and "40×80" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.148：如何验证｜") and "同一模具只返回一行" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.147：本次更新｜") and "实体组件" in item and "生产任务单" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.147：本次更新｜") and "子件交付" in item and "当前常用箱" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.147：本次更新｜") and "父件交付" in item and "父件标签" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.147：如何验证｜") and "v0.22.147" in item and "vv30v8x9z19" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.146：本次更新｜") and "Z.001.000206" in item and "90张" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.146：如何验证｜") and "v0.22.146" in item and "vv30v8x9z19" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.145：本次更新｜") and "产品规格" in item and "开料方式" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.145：本次更新｜") and "瑞明#7" in item and "瑞明#11" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.145：如何验证｜") and "v0.22.145" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.144：本次更新｜") and "真实宽40mm、高80mm纸型" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.144：如何验证｜") and "v0.22.144" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.143：本次更新｜") and "Landscape" in item and "40mm 打印头" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.143：如何验证｜") and "v0.22.143" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.142：本次更新｜") and "只输出一次" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.142：如何验证｜") and "v0.22.142" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.141：本次更新｜") and "历史报料明细" in item and "当前真实收料路线" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.141：如何验证｜") and "v0.22.141" in item and "vv30v8x9z19" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.140：本次更新｜") and "Gprinter" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.140：如何验证｜") and "v0.22.140" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.139：本次更新｜") and "子件交付" in item and "父件交付" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.139：如何验证｜") and "v0.22.139" in item and "vv30v8x9z19" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.138：本次更新｜") and "待收料接口" in item and "HTTP响应头" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.138：如何验证｜") and "v0.22.138" in item and "uu29v8x9z18" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.137：本次更新｜") and "80×40页面" in item and "40×30" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.137：如何验证｜") and "v0.22.137" in item and "uu29v8x9z18" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.136：本次更新｜") and "40mm打印头" in item and "旋转90度" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.136：如何验证｜") and "v0.22.136" in item and "uu29v8x9z18" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.135：本次更新｜") and "embedded=1" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.135：如何验证｜") and "HTTP 422" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.134：本次更新｜") and "规格mm" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.134：如何验证｜") and "v0.22.134" in item and "uu29v8x9z18" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.133：本次更新｜") and "订单、报料、来料、生产、送货" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.133：如何验证｜") and "v0.22.133" in item and "tt28v8x9z17" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.132：本次更新｜") and "K9C7J-AB/EB / AB" in item and "K9C7J / AB" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.132：如何验证｜") and "v0.22.132" in item and "tt28v8x9z17" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.131：本次更新｜") and "MK005" in item and "暂不报料" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.131：如何验证｜") and "v0.22.131" in item and "tt28v8x9z17" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.130：本次更新｜") and "虚拟组合父件" in item and "实体组件" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.130：如何验证｜") and "v0.22.130" in item and "tt28v8x9z17" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.129：本次更新｜") and "同步对应组件常用箱" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.129：如何验证｜") and "v0.22.129" in item and "tt28v8x9z17" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.128：本次更新｜") and "分多次报料" in item and "剩余数量" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.128：如何验证｜") and "v0.22.128" in item and "ss27v8x9z16" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.127：本次更新｜") and "已有包装标签张数" in item and "升级" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.127：如何验证｜") and "v0.22.127" in item and "ss27v8x9z16" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.126：本次更新｜") and "同一订单" in item and "额外库存抵扣" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.126：如何验证｜") and "v0.22.126" in item and "ss27v8x9z16" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.125：本次更新｜") and "累计正式报料" in item and "冻结需求" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.125：如何验证｜") and "v0.22.125" in item and "ss27v8x9z16" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.124：本次更新｜") and "盖片" in item and "订单级" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.124：如何验证｜") and "v0.22.124" in item and "ss27v8x9z16" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.123：本次更新｜") and "三选一" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.123：如何验证｜") and "v0.22.123" in item and "pp24v8x9z13" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.122：本次更新｜") and "部分正式报料" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.122：如何验证｜") and "v0.22.122" in item and "pp24v8x9z13" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.121：本次更新｜") and "模具编号" in item and "本次扫码已登记" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.121：如何验证｜") and "v0.22.121" in item and "pp24v8x9z13" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.119：本次更新｜") and "盖底尺寸" in item and "合并显示" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.119：如何验证｜") and "v0.22.119" in item and "oo23v8x9z12" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.118：本次更新｜") and "cover/base" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.118：如何验证｜") and "v0.22.118" in item and "oo23v8x9z12" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.117：本次更新｜") and "履约备忘" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.117：如何验证｜") and "v0.22.117" in item and "oo23v8x9z12" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.115：本次更新｜") and "移动该模具" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.115：如何验证｜") and "mm21v8x9z10" in item for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.110：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.110：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.108：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.108：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.107：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.107：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.106：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.106：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.105：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.105：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.104：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.104：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.103：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.103：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.102：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.102：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.101：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.101：如何验证｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.100：本次更新｜") for item in APP_CHANGELOG)
    assert any(
        item.startswith("v0.22.100：如何验证｜")
        and "v0.22.100" in item
        and "ll20" in item
        for item in APP_CHANGELOG
    )
    assert any(item.startswith("v0.22.99：本次更新｜") for item in APP_CHANGELOG)
    assert any(item.startswith("v0.22.96：本次更新｜") for item in APP_CHANGELOG)
    assert any(
        item.startswith("v0.22.98：本次更新｜")
        and "外购包装采购单" in item
        and "客户订单" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.98：如何验证｜")
        and "v0.22.98" in item
        and "kk19" in item
        for item in APP_CHANGELOG
    )
    assert any(item.startswith("v0.22.94：本次更新｜") for item in APP_CHANGELOG)
    assert any(
        item.startswith("v0.22.86：本次更新｜")
        and "常用箱" in item
        and "即时搜索" in item
        and "旧缓存" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.88：本次更新｜")
        and "一张天明实测仓库地图" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.84：本次更新｜")
        and "A1 型纸箱" in item
        and "模切内盒" in item
        and "衬板" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.83：本次更新｜")
        and "模具名称" in item
        and "拼音首字母" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.82：本次更新｜")
        and "顶部显示模式行" in item
        and "仓库" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.82：如何验证｜")
        and "v0.22.82" in item
        and "ea09" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.81：本次更新｜")
        and "全部真实待送订单候选" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.80：本次更新｜")
        and "组合 BOM" in item
        and "冻结快照" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.80：如何验证｜")
        and "v0.22.80" in item
        and "ea09" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.75：本次更新｜") and "预计总成本快照" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.75：如何验证｜") and "v0.22.75" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.74：本次更新｜") and "双拼报料" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.74：如何验证｜") and "v0.22.74" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.73：本次更新｜") and "外购包装" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.73：如何验证｜") and "v0.22.73" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "如何验证｜" in item and "v0.22.163" in item and "de39" in item
        for item in current_release
    )
    assert any(
        item.startswith("v0.22.79：本次更新｜") and "完整工作区高度" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.79：如何验证｜") and "v0.22.79" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.78：本次更新｜") and "库存台账" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.78：如何验证｜") and "sr2" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.77：本次更新｜") and "低配电脑" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.77：如何验证｜") and "v0.22.77" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.61：")
        and "默认进入二维平面" in item
        and "轻量批量绘制" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.58：") and "待报料" in item and "页签条件隐藏" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.58：如何验证｜")
        and "15 条" in item
        and "昆山鸣朋 10 条" in item
        for item in APP_CHANGELOG
    )
    assert any(
        item.startswith("v0.22.51：") and "YKE 100 条" in item and "KEW 31 条" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u5ba2\u6237" in item
        and "\u5e38\u7528\u7bb1" in item
        and "\u6750\u8d28" in item
        and "\u7248\u672c\u5386\u53f2" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u4fee\u6539\u539f\u56e0" in item
        and "\u64cd\u4f5c\u8005" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u5e76\u53d1\u51b2\u7a81" in item
        and "409" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u5f02\u5e38\u53d8\u66f4" in item
        and "\u4e8c\u6b21\u786e\u8ba4" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u7ba1\u7406\u5458\u6062\u590d" in item
        and "\u65b0\u7248\u672c" in item
        and "\u6062\u590d\u539f\u56e0" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u654f\u611f\u4ef7\u683c" in item
        and "\u6743\u9650" in item
        and "\u8131\u654f" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u6587\u672c\u5c42" in item
        and "OCR" in item
        and "\u6570\u91cf" in item
        and "\u91d1\u989d" in item
        for item in APP_CHANGELOG
    )
    assert any(
        "\u53ea\u9700\u9009\u62e9\u5ba2\u6237" in item
        and "\u4e09\u697c\u5177\u4f53\u8d27\u4f4d" in item
        for item in APP_CHANGELOG
    )
    assert any("396" in item and "\u9884\u8bbe\u8d27\u4f4d" in item for item in APP_CHANGELOG)
    assert any("F2" in item and "F3" in item and "F4" in item for item in APP_CHANGELOG)
    assert any(
        "\u6309\u6574\u5f20\u9001\u8d27\u5355\u9009\u62e9" in item
        and "\u5168\u90e8\u660e\u7ec6" in item
        for item in APP_CHANGELOG
    )
    assert any("\u6708\u7ed3\u7ed3\u8f6c\u65e5" in item and "20" in item for item in APP_CHANGELOG)
    assert any("\u5f85\u5bf9\u8d26" in item and "\u6309\u5ba2\u6237+\u6708\u4efd\u6c47\u603b" in item for item in APP_CHANGELOG)
    assert any("\u4e00\u952e\u542f\u52a8" in item for item in APP_CHANGELOG)
    assert any("\u684c\u9762\u56fe\u6807\u5c31\u80fd\u81ea\u52a8\u5347\u7ea7" in item for item in APP_CHANGELOG)


def test_release_script_is_two_phase_and_requires_exact_approval() -> None:
    for marker in (
        "-Prepare",
        "-Apply",
        "ExpectedCodeSha",
        "ExpectedRevision",
        "ExpectedAppVersion",
        "ApprovalToken",
        "factory-current-baseline",
        "Stop-ErpService",
        "Assert-ErpStopped",
        "ERP_ENVIRONMENT=production",
        "release_erp.py",
        "mark-started",
        "check-release-metadata",
    ):
        assert marker in RELEASE_SCRIPT
    prepare_block = RELEASE_SCRIPT.index("if ($Prepare)")
    assert RELEASE_SCRIPT.index(
        '"check-release-metadata",',
        prepare_block,
    ) < RELEASE_SCRIPT.index("Stop-ErpService", prepare_block)
    assert RELEASE_SCRIPT.index("Stop-ErpService") < RELEASE_SCRIPT.index(
        '"prepare",'
    )
    assert "git fetch" not in RELEASE_SCRIPT
    assert "git merge" not in RELEASE_SCRIPT


def test_release_helper_contains_backup_rehearsal_and_fail_closed_checks() -> None:
    for marker in (
        "mode=ro",
        "source_connection.backup",
        "PRAGMA integrity_check",
        "PRAGMA foreign_key_check",
        "core_counts",
        "awaiting_human_approval",
        "apply_failed_service_must_remain_stopped",
        "approval_token",
        "release_metadata",
        "expected_app_version",
        "human_acceptance_status",
        "check-release-metadata",
        '"alembic", "upgrade", revision',
    ):
        assert marker in RELEASE_HELPER
    assert '"head"' not in RELEASE_HELPER


def test_release_helper_creates_valid_sqlite_copy(tmp_path: Path) -> None:
    from scripts.admin.release_erp import create_sqlite_copy

    source = tmp_path / "carton_erp.sqlite3"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('test-revision')")
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('工厂更新测试')")
        connection.commit()

    backup = backup_dir / "copy.sqlite3"
    result = create_sqlite_copy(source, backup)

    assert backup.is_file()
    assert result["integrity_check"] == "ok"
    assert result["foreign_key_violations"] == 0
    assert result["revision"] == "test-revision"
    assert len(result["sha256"]) == 64
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "工厂更新测试"
