from app.services.drawing_exports import dxf, engineering_pdf_1to1
from app.services.drawing_geometry import build_geometry


def test_release_vector_exports_preserve_mm_layers():
    geometry = build_geometry('liner_v1', {'length_mm': '100', 'width_mm': '50'})
    exported = dxf(geometry).decode('ascii')
    assert '$INSUNITS\r\n70\r\n4' in exported
    assert 'CUT' in exported and 'SCORE' in exported
    pdf = engineering_pdf_1to1(geometry)
    assert pdf.startswith(b'%PDF')
