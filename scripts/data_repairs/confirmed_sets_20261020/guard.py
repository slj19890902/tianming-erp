"""Compare every table and constrain changed rows before committing."""
import hashlib,json
from operation import canonical
MUTABLE = {'inventory_lots','finished_goods_inventory_details','inventory_reservations','inventory_movements','operation_logs','production_tasks','sqlite_sequence'}
def capture(connection):
    result = {}
    for (name,) in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = '"' + name.replace('"','""') + '"'
        cursor = connection.exec_driver_sql('SELECT * FROM '+quoted)
        columns = list(cursor.keys())
        hashes = []
        rows = []
        for row in cursor:
            raw = tuple(row)
            hashes.append(hashlib.sha256(canonical(raw).encode()).hexdigest())
            if name in MUTABLE: rows.append(dict(zip(columns,raw)))
        result[name] = dict(hash=hashlib.sha256(''.join(sorted(hashes)).encode()).hexdigest(),count=len(hashes))
        if name in MUTABLE: result[name]['rows'] = rows
    return result

def verify(before,after):
    assert before.keys()==after.keys()
    changed=[]
    for name, prior in before.items():
        current=after[name]
        if current['hash']==prior['hash']:continue
        assert name in MUTABLE, 'Unexpected changed table: '+name
        changed.append(name)
        if name=='sqlite_sequence':continue
        pk='inventory_lot_id' if name=='finished_goods_inventory_details' else 'id'
        old={r[pk]:r for r in prior['rows']};new={r[pk]:r for r in current['rows']}
        assert old.keys() <= new.keys(), 'Deleted rows: '+name
        altered={key for key in old if old[key]!=new[key]};added=new.keys()-old.keys()
        if name=='inventory_lots':
            assert altered=={1148,1220} and not added
            for key in altered:
                differences={f for f in old[key] if old[key][f]!=new[key][f]}
                assert differences <= {'quantity_available','quantity_reserved','version','updated_at','last_movement_at'}
        elif name=='finished_goods_inventory_details':
            assert altered=={1148,1220} and not added
            for key in altered:assert {f for f in old[key] if old[key][f]!=new[key][f]}=={'physical_basis_json'}
        elif name=='production_tasks':
            assert not added
            assert all(old[key]['order_item_id']==10902 for key in altered)
            allowed={'status','planned_quantity','finished_coverage_snapshot','ordered_quantity_snapshot','material_received_quantity','material_input_quantity','readiness_basis','version','updated_at'}
            assert all({f for f in old[key] if old[key][f]!=new[key][f]} <= allowed for key in altered)
        else:
            assert not altered
            assert len(added)=={'inventory_reservations':1,'inventory_movements':3,'operation_logs':3}[name]
    assert {'inventory_lots','finished_goods_inventory_details','inventory_reservations','inventory_movements','operation_logs'} <= set(changed)
    return dict(tables_checked=len(before), changed_tables=sorted(changed), unrelated_rows_unchanged=True)
