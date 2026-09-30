"""Explicit public edit contract; hidden master fields never cross the browser."""
FIELDS = {
    "customer_update": {
        "name":"客户全称", "chinese_short_name":"中文简称", "contact_person":"联系人",
        "phone":"电话", "address":"地址", "delivery_method":"送货方式",
        "payment_term_days":"账期天数", "invoice_title":"开票抬头", "tax_no":"税号",
        "bank_account":"银行账号", "remark":"备注",
    },
    "product_update": {
        "product_name":"产品名称", "customer_material_code":"存货编码", "customer_model":"客户型番",
        "customer_product_name":"客户用途", "material_id":"材质代码", "flute_type":"楞型",
        "layer_count":"层数", "length_mm":"长 mm", "width_mm":"宽 mm", "height_mm":"高 mm",
        "sale_unit_price":"销售单价", "report_length_mm":"报料长 mm", "report_width_mm":"报料宽 mm",
        "production_notes":"生产备注", "remark":"备注",
    },
    "order_update": {"customer_po":"客户单号", "delivery_date":"交货日期", "remark":"备注"},
}

def hydrate(db, action, target_id, patch):
    from app.services.business_approvals import specs
    from fastapi import HTTPException
    schema, model, _, _ = specs(action)
    obj = db.get(model, target_id)
    if obj is None:
        raise HTTPException(404, "申请关联资料不存在")
    if set(patch) - set(FIELDS[action]) - {"expected_version"}:
        raise HTTPException(422, "申请包含不支持修改的字段")
    if hasattr(obj, "version") and patch.get("expected_version") != obj.version:
        raise HTTPException(409, "资料版本已变化，请重新打开申请")
    data = {key:getattr(obj,key) for key in schema.model_fields if hasattr(obj,key)}
    data.update(patch)
    if hasattr(obj,"version"):
        data["expected_version"] = obj.version
    return schema.model_validate(data)
