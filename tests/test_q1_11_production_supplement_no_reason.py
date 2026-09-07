from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_API = (ROOT / "app" / "api" / "production.py").read_text(encoding="utf-8")
PRODUCTION_SERVICE = (ROOT / "app" / "services" / "production_workflow.py").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _supplement_method() -> str:
    start = INDEX.index("async supplementProductionCompletion(row)")
    end = INDEX.index("openProductionInventory(row)", start)
    return INDEX[start:end]


def test_supplement_requires_business_facts_but_not_free_text_reason() -> None:
    method = _supplement_method()
    assert 'type:"productionSupplement"' in method
    assert "本次实际合格产量" in INDEX
    assert 'v-model.number="productionSupplementForm.location_id"' in INDEX
    assert "productionLocationsForRow(row).find" in method
    assert "补充生产确认原因" not in method
    assert "prompt(" not in method
    assert "confirm(" not in method
    assert "material_input_quantity:input" in method
    assert "actual_output_quantity:output" in method
    assert "defective_quantity:theoretical-output" in method
    assert "location_id:Number(location.id)" in method
    assert "remarks:null" in method


def test_backend_keeps_supplement_permissions_quantities_and_audit() -> None:
    assert 'item.completion_type == "supplemental"' in PRODUCTION_API
    assert 'detail="补充生产确认仅限管理员"' in PRODUCTION_API
    assert '"SUPPLEMENT_PRODUCTION_COMPLETE"' in PRODUCTION_API
    assert 'description=("管理员补充生产确认"' in PRODUCTION_API
    assert "material_input_quantity" in PRODUCTION_API
    assert "actual_output_quantity" in PRODUCTION_API
    assert "defective_quantity" in PRODUCTION_API
    assert "command.material_input_quantity <= 0" in PRODUCTION_SERVICE
    assert "command.actual_output_quantity <= 0" in PRODUCTION_SERVICE
    assert "command.defective_quantity < 0" in PRODUCTION_SERVICE
    assert "available_input" in PRODUCTION_SERVICE
    assert "expected_version" in PRODUCTION_SERVICE


def test_supplement_entry_uses_the_existing_backend_eligibility() -> None:
    assert 'v-if="canAdmin && row.can_supplement"' in INDEX
    assert ':disabled="productionBusy" @click="supplementProductionCompletion(row)"' in INDEX
    assert '"can_supplement": (' in PRODUCTION_SERVICE
    assert 'completion.status == "posted"' in PRODUCTION_SERVICE
    assert "and available_input > 0" in PRODUCTION_SERVICE
    assert "not receipt_purpose_managed" in PRODUCTION_SERVICE
