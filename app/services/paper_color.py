"""Face color comes from the supplier's first (outer-face) paper-code character."""
import unicodedata
from sqlalchemy import select
from app.models.supplier_paper_code import SupplierPaperCode


def material_face(db, material=None, *, material_code=None, supplier_name=None):
    code = unicodedata.normalize("NFKC", str(material.code if material else material_code or "")).strip().upper()
    supplier = str(material.supplier_name if material else supplier_name or "").strip()
    if code and supplier:
        color = db.scalar(select(SupplierPaperCode.color).where(
            SupplierPaperCode.supplier_name == supplier, SupplierPaperCode.code_char == code[0]))
        if color is not None:
            return color
    if material:
        return "white" if material.is_white_face else "kraft"
    return "kraft"
