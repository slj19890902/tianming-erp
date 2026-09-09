import hashlib
from pathlib import Path
import pytest
from scripts.admin.audit_multilevel_bom_transition import audit
from tests.test_multilevel_bom_orders import context
from tests.test_multilevel_bom_master import configure_liner


def test_transition_audit_is_read_only_and_tracks_remaining_order(context):
    db, actor, item, _ = context
    configure_liner(db,actor)
    item.delivered_quantity = 40
    db.commit()
    path = db.get_bind().url.database
    before_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    first = audit(path,[1])
    assert {r["id"] for r in first["products"]} == {1,2,3,4}
    assert first["unfinished_orders"][0]["quantity"] == 100
    assert first["unfinished_orders"][0]["delivered_quantity"] == 40
    assert audit(path,[1]) == first
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == before_hash
    item.is_force_closed = True
    db.commit()
    assert audit(path,[1])["unfinished_orders"] == []


@pytest.mark.parametrize("ids", [[],[0],[True]])
def test_transition_audit_requires_explicit_identity(context,ids):
    db, *_ = context
    with pytest.raises(ValueError):
        audit(db.get_bind().url.database,ids)
