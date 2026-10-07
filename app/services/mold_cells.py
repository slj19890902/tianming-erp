"""Durable physical mold cell identities in the existing versioned layout ledger."""
from __future__ import annotations
import re
from uuid import UUID, uuid4

CELL_PREFIX = "MCELL-"
RACK_CODE_PATTERN = re.compile(r"^[A-Z]{1,8}$")

def cell_code(cell_id: str) -> str:
    return CELL_PREFIX + str(UUID(str(cell_id))).upper()

def reconcile_mold_cells(rack: dict, previous: dict | None = None) -> dict:
    code = str(rack.get("mold_rack_code") or "").strip().upper()
    if previous and previous.get("mold_rack_code") and str(previous["mold_rack_code"]).strip().upper() != code:
        raise ValueError("已有模具架号不能改名或切换地址类型，请保留稳定架号")
    if not code or not RACK_CODE_PATTERN.fullmatch(code):
        return rack  # Historical R01-R04 retain their original address contract.
    old = previous or {}
    if old.get("mold_rack_code") and old["mold_rack_code"] != code:
        raise ValueError("已有模具架号不能改名，请保留稳定架号")
    counts = rack.get("level_cell_counts") or [int(rack.get("bays") or 1)] * int(rack["levels"])
    if any(int(n) < 1 for n in counts):
        raise ValueError("模具货架每层至少配置一格")
    wanted = {(level, grid) for level,n in enumerate(counts,1) for grid in range(1,int(n)+1)}
    existing = {str(c["id"]):c for c in old.get("mold_cells") or []}
    retired = [dict(c) for c in old.get("retired_mold_cells") or []]
    supplied = rack.get("mold_cells")
    if previous is None and supplied:
        raise ValueError("新增模具格身份由系统生成，不能指定旧身份")
    if supplied is None:
        cells = [dict(c) for c in existing.values() if (int(c["level"]),int(c["grid"])) in wanted]
    else:
        cells = [dict(c) for c in supplied]
    used_ids=set(); used_slots=set(); used_aliases=set()
    retired_ids={str(c["id"]) for c in retired}
    retired_aliases={str(c["alias"]).upper() for c in retired}
    for c in cells:
        c["id"] = str(UUID(str(c["id"])))
        c["level"]=int(c["level"]); c["grid"]=int(c["grid"])
        c["alias"]=str(c["alias"]).strip().upper()
        slot=(c["level"],c["grid"])
        if c["id"] in existing and slot != (int(existing[c["id"]]["level"]),int(existing[c["id"]]["grid"])):
            raise ValueError("已保存的模具格身份不能重新绑定其他物理层格")
        if c["alias"] in retired_aliases:
            raise ValueError("已停用格的历史短号不能重新使用")
        if c["id"] in used_ids or slot in used_slots or c["alias"] in used_aliases:
            raise ValueError("模具格身份、层格和短号不能重复")
        if slot not in wanted or not re.fullmatch(re.escape(code)+r"[1-9]\d*",c["alias"]):
            raise ValueError("模具格短号或层格超出当前架结构")
        if c["id"] in retired_ids or (previous and c["id"] not in existing):
            raise ValueError("不能替换或复用已保存的模具格身份；新增格由系统生成")
        used_ids.add(c["id"]); used_slots.add(slot); used_aliases.add(c["alias"])
    if supplied is not None:
        retained={key for key,c in existing.items() if (int(c["level"]),int(c["grid"])) in wanted}
        if not retained.issubset(used_ids):
            raise ValueError("现存物理层格必须保留原格身份，只能调整显示短号")
    retired.extend(dict(c) for key,c in existing.items() if key not in used_ids)
    reserved=used_aliases | {str(c["alias"]) for c in retired}
    sequence=1
    for level,grid in sorted(wanted-used_slots):
        while f"{code}{sequence}" in reserved: sequence+=1
        alias=f"{code}{sequence}"; reserved.add(alias)
        cells.append({"id":str(uuid4()),"level":level,"grid":grid,"alias":alias})
    rack["mold_rack_code"]=code
    rack["mold_cells"]=sorted(cells,key=lambda c:(c["level"],c["grid"]))
    rack["retired_mold_cells"]=retired
    return rack

def projected_cells(rack: dict) -> list[dict]:
    return [{**c,"location_code":cell_code(c["id"]),"location_id":c["id"],"short_label":c["alias"]}
            for c in rack.get("mold_cells") or []]
