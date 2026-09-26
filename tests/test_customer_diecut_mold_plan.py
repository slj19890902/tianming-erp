import sqlite3
import pytest
from fastapi.testclient import TestClient
from tests.test_p1_82_mold_multi_customer import p182_app, _login, _single_customer_payload


@pytest.mark.parametrize('name', ['TRD-NϵP', 'T1K-08,-16(I/O)', 'KCM，KCX'])
def test_product_name_survives_formal_mold_create_and_replay(p182_app, name):
    app, factory = p182_app
    with TestClient(app) as client:
        _login(client, 'admin')
        payload = _single_customer_payload(customer_id=1, key='diecut-name-preservation', label_name='80010637')
        payload.update(chinese_short_name=name, rack_location='1F-M-R04')
        created = client.post('/api/warehouse/molds', json=payload)
        assert created.status_code == 201, created.text
        assert created.json()['chinese_short_name'] == name
        replay = client.post('/api/warehouse/molds', json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()['id'] == created.json()['id']
        assert replay.json()['idempotent_replay']


@pytest.mark.parametrize('name', ['<script>alert(1)</script>', 'abc\x00def', 'a'*101])
def test_mold_short_name_still_rejects_markup_controls_and_overflow(name):
    from app.services.mold_identity import normalize_mold_chinese_short_name, MoldIdentityError
    with pytest.raises(MoldIdentityError):
        normalize_mold_chinese_short_name(name)


def test_plan_is_readonly_reuses_bindings_and_detects_shared_and_changed_facts():
    from scripts.audit.customer_diecut_molds import build_plan
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE customers(id,customer_code,chinese_short_name);
        CREATE TABLE products(id,version,customer_id,product_code,product_name,
            box_category,production_process,is_active,deleted_at,mold_tool_id);
        CREATE TABLE mold_tools(id,mold_code,mold_name,label_name,chinese_short_name,
            rack_location,version,location_version,is_active,archive_status);
        CREATE TABLE mold_tool_customers(mold_tool_id,customer_id);
        INSERT INTO customers VALUES(1,'YG','研光'),(2,'OTHER','其他');
        INSERT INTO products VALUES
            (1,1,1,'SKU1','KCM，KCX','die_cut','模切',1,NULL,NULL),
            (2,1,1,'SKU2','旧模','die_cut','模切',1,NULL,10),
            (3,1,1,'SKU3','停用','die_cut','模切',0,NULL,NULL),
            (4,1,2,'SKU4','范围外','die_cut','模切',1,NULL,NULL);
        INSERT INTO mold_tools VALUES(10,'M10','旧模','SKU2','旧模','1F-M-R04',1,1,1,'active');
        INSERT INTO mold_tool_customers VALUES(10,1);
        ALTER TABLE products ADD COLUMN customer_material_code;
    ''')
    before = db.total_changes
    first = build_plan(db)
    assert db.total_changes == before
    assert first['summary'] == {'create_and_bind': 1, 'reuse_bound': 1, 'review': 1}
    assert first == build_plan(db)
    # Main box and liner may share an internal code but have distinct inventory codes.
    db.execute("INSERT INTO products VALUES(5,1,1,'SKU1','内衬','normal','粘贴',1,NULL,NULL,'SKU1内衬')")
    assert build_plan(db)['summary'] == first['summary']
    db.execute("UPDATE products SET customer_material_code='SKU1' WHERE id=5")
    assert '客户存货编码非唯一' in build_plan(db)['items'][0]['review_reasons']
    db.execute('DELETE FROM products WHERE id=5')
    db.execute("INSERT INTO mold_tools VALUES(11,'OLD-SKU1','旧模具SKU1',NULL,NULL,'1F-M-R04',1,1,1,'active')")
    collision = build_plan(db)['items'][0]
    assert collision['action'] == 'review' and collision['candidate_mold_ids'] == [11]
    db.execute('DELETE FROM mold_tools WHERE id=11')
    db.execute('UPDATE products SET version=2 WHERE id=1')
    assert build_plan(db)['source_fingerprint'] != first['source_fingerprint']
    db.execute('UPDATE products SET mold_tool_id=10 WHERE id=1')
    shared = build_plan(db)
    assert shared['summary'] == {'review': 3}
    assert all('共享模具' in r['review_reasons'][0] for r in shared['items'][:2])
    db.close()
