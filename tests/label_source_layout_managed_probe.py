"""Run only through scripts/uat/run_task.py against its owned database copy."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
Path(os.environ["ERP_UAT_ROOT"], "customer-document-review.json").write_text('{"entries":[]}')
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.supplier_requisition_order import SupplierRequisitionOrder
from app.models.product import Product
from app.services.production_packaging_label import (
    build_supplier_requisition_packaging_label_package,
)
from app.services.production_label_operations import (
    ProductionLabelOperationError,
    prepare_packaging_label_job,
    get_packaging_label_job,
    confirm_packaging_label_job_printed,
)


def main() -> None:
    database = os.environ["ERP_DATABASE_PATH"]
    assert os.environ["ERP_ENVIRONMENT"] == "test"
    assert "/copies/" in database
    engine = create_engine("sqlite:///" + database)
    with Session(engine) as db:
        order = db.get(SupplierRequisitionOrder, 192)
        for size in ((40, 30), (40, 80), (80, 40)):
            sized = build_supplier_requisition_packaging_label_package(
                db, order, selected_supplier_item_ids={598}, paper_size=size
            )
            assert sized["printable"]
            assert sized["template_dimensions"] == {
                "width_mm": size[0], "height_mm": size[1]
            }
            assert sized["plans"][0]["total_quantity"] == 1560
        jsd_order = db.get(SupplierRequisitionOrder, 171)
        for size in ((40, 30), (40, 80), (80, 40)):
            jsd = build_supplier_requisition_packaging_label_package(
                db, jsd_order, selected_supplier_item_ids={494}, paper_size=size
            )
            assert jsd["printable"]
            elements = {row["id"]: row for row in jsd["label_layout"]["layout"]["elements"]}
            assert elements["product_name"]["visible"] is False
            assert elements["quantity"]["visible"] is True
            assert elements["product_code"]["width_mm"] == 39 * size[0] / 40
        package = build_supplier_requisition_packaging_label_package(
            db, order, selected_supplier_item_ids={598}, paper_size=(40, 30)
        )
        assert package["printable"] and len(package["plans"]) == 1
        plan = package["plans"][0]
        assert plan["selection_key"] == "supplier_stock:598:23"
        assert plan["production_task_id"] is None
        assert plan["total_quantity"] == 1560
        assert plan["label_count"] == 312
        prepared = prepare_packaging_label_job(
            db, order=order, idempotency_key="label-598-managed-test-v1",
            expected_plan_fingerprint=package["plan_fingerprint"],
            requested_print_counts={plan["selection_key"]: 1},
            selected_supplier_item_ids={598}, paper_size=(40, 30), operator_id=1,
        )
        assert prepared.package["label_count"] == 1
        assert prepared.package["labels"][0]["quantity"] == 5
        db.commit()
        repeated = prepare_packaging_label_job(
            db, order=order, idempotency_key="label-598-managed-test-v1",
            expected_plan_fingerprint=package["plan_fingerprint"],
            requested_print_counts={plan["selection_key"]: 1},
            selected_supplier_item_ids={598}, paper_size=(40, 30), operator_id=1,
        )
        assert repeated.replayed and repeated.job.id == prepared.job.id
        try:
            prepare_packaging_label_job(
                db, order=order, idempotency_key="label-598-managed-test-v1",
                expected_plan_fingerprint=package["plan_fingerprint"],
                requested_print_counts={plan["selection_key"]: 2},
                selected_supplier_item_ids={598}, paper_size=(40, 30), operator_id=1,
            )
        except ProductionLabelOperationError as error:
            assert "幂等键" in str(error)
        else:
            raise AssertionError("different payload reused the same print-job key")
        loaded = get_packaging_label_job(db, prepared.job.id)
        assert loaded.package["label_count"] == 1
        confirmed = confirm_packaging_label_job_printed(
            db, job_id=prepared.job.id,
            confirmation_key="label-598-managed-confirm-v1", operator_id=1,
        )
        assert confirmed.job.status == "printed"
        db.commit()
        replay = get_packaging_label_job(db, prepared.job.id)
        assert replay.package["labels"][0]["selection_key"] == "supplier_stock:598:23"
        product = db.get(Product, 3460)
        product.production_label_units_per_label = 7
        db.flush()
        tail = build_supplier_requisition_packaging_label_package(
            db, order, selected_supplier_item_ids={598}
        )
        assert tail["plans"][0]["total_quantity"] == 1560
        assert tail["labels"][-1]["quantity"] == 6
        db.rollback()
    print("PASS: 598 source -> prepare -> read -> confirm -> frozen reprint; JSD and three paper sizes")


if __name__ == "__main__":
    main()
