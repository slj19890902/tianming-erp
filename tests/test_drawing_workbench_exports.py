import pytest

from app.services.drawing_exports import dxf, engineering_pdf, engineering_pdf_1to1
from app.services.drawing_geometry import build_geometry


def test_release_vector_exports_preserve_mm_layers():
    geometry = build_geometry('liner_v1', {'length_mm': '100', 'width_mm': '50'})
    import ezdxf
    from io import StringIO
    doc = ezdxf.read(StringIO(dxf(geometry).decode('utf-8')))
    assert doc.units == 4 and doc.dxfversion == 'AC1024'
    assert len(doc.modelspace()) == len(geometry['cut'])
    assert {entry.dxf.layer for entry in doc.modelspace()} == {'CUT'}
    assert not doc.audit().errors
    assert doc.layers.get('SCORE').dxf.linetype == 'DASHED'
    pdf = engineering_pdf_1to1(geometry)
    assert pdf.startswith(b'%PDF')
    # 50mm blank + two 10mm margins is widened to retain a physical 100mm ruler.
    assert b'/MediaBox [ 0 0 340.1575' in pdf


def test_assembly_has_component_list_but_no_fake_blank_exchange_export():
    assembly = {'type': 'assembly', 'width_mm': '1', 'height_mm': '1', 'cut': [], 'score': [], 'panels': [], 'dimensions': {}}
    metadata = {'parameters': {'__drawing_workbench_v1': {'editor_state': {'assembly': {'placements': [{
        'path': '1/2', 'product_id': 88, 'child_release_id': 12, 'position_mm': ['1', '2', '3'],
    }]}}}}}
    pdf = engineering_pdf(assembly, customer='UAT', product='组合件', number='TM-A', revision='R01',
                          thickness='无自身纸板', print_objects=[], drawing_metadata=metadata)
    assert pdf.startswith(b'%PDF')
    with pytest.raises(ValueError, match='组合图'):
        engineering_pdf_1to1(assembly)
    with pytest.raises(ValueError, match='组合图'):
        dxf(assembly)
