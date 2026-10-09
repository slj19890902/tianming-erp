"""Real HTTP and PDF issuer isolation; fictional company B and visibly void seal."""
import base64
import json
from pathlib import Path
import pytest
from sqlalchemy import select, func
from tests.test_contract_seal import contract_pdf_app, original_contract_pdf_app, _login, _test_image, _pdf_text


def _settings(client):
    r = client.get('/api/system/company')
    assert r.status_code == 200, r.text
    return r.json()


def _create(client, name='虚构乙包装有限公司'):
    r = client.post('/api/system/companies', json=dict(company_name=name, selection_version=_settings(client)['selection_version']))
    assert r.status_code == 201, r.text
    return r.json()['company']


def _activate(client, cid):
    s = _settings(client)
    row = next(x for x in s['profiles'] if x['id'] == cid)
    return client.post(f'/api/system/companies/{cid}/activate', json=dict(expected_version=row['version'], selection_version=s['selection_version']))


def _edit(client, cid, **changes):
    s = _settings(client)
    row = next(x for x in s['profiles'] if x['id'] == cid)
    return client.put(f'/api/system/companies/{cid}', json=dict(row, **changes, expected_version=row['version'], selection_version=s['selection_version']))


def _seal(client, cid):
    s = client.get(f'/api/system/company/seal/settings?company_id={cid}').json()
    return client.post(f'/api/system/company/seal/settings?company_id={cid}', json=dict(company_version=s['company_version'], expected_version=s['version'], image_base64=base64.b64encode(_test_image()).decode(), size_mm=40))


def _new_contract(client, cid):
    original = client.get(f'/api/contracts/{cid}').json()
    r = client.post('/api/contracts', json=dict(customer_id=original['customer_id'], contract_date='2026-10-09', items=[dict(product_name='虚构测试纸箱', quantity=10, unit_price='2.50')]))
    assert r.status_code == 201, r.text
    return r.json()


def _export(client, cid, version, key):
    s = client.get(f'/api/contracts/seal/{cid}/settings').json()
    return client.post(f'/api/contracts/seal/{cid}/pdf', json=dict(expected_version=version, seal_version=max(s['version'],1), company_id=s['company_id'], company_version=s['company_version'], operation_key=key))


def test_companies_seals_and_contract_headers_stay_separate(contract_pdf_app):
    client, factory, ids = contract_pdf_app
    _login(client, 'p135a-admin')
    original = _settings(client)['company_name']
    assert _seal(client, 1).status_code == 200
    a = _new_contract(client, ids['own'])
    issued = _export(client, a['id'], 1, 'company-A-contract-001')
    assert issued.status_code == 200
    b = _create(client)
    assert _activate(client, b['id']).status_code == 200
    # Both old and new saved A contracts keep A; B never inherits its seal.
    for cid in [ids['own'], a['id']]:
        assert client.get(f'/api/contracts/{cid}/print').json()['sender']['company_name'] == original
    no_seal = client.get(f'/api/system/company/seal/settings?company_id={b["id"]}').json()
    assert no_seal['available'] is False
    b_contract = _new_contract(client, ids['own'])
    assert b_contract['issuer_company_name'] == b['company_name']
    assert _export(client, b_contract['id'], 1, 'company-B-no-seal-001').status_code == 409
    assert _seal(client, b['id']).status_code == 200
    b_pdf = _export(client, b_contract['id'], 1, 'company-B-contract-001')
    assert b_pdf.status_code == 200
    assert b['company_name'] in ''.join(_pdf_text(b_pdf.content)[1])
    assert _activate(client, 1).status_code == 200
    assert _export(client, a['id'], 1, 'company-A-contract-001').content == issued.content
    assert client.get(f'/api/contracts/{b_contract["id"]}/print').json()['sender']['company_name'] == b['company_name']
    assert _settings(client)['company_name'] == original
    from app.models.contract_seal import ContractSealedExport
    with factory() as db:
        records=list(db.scalars(select(ContractSealedExport)))
        assert len(records)==2 and len({r.seal_id for r in records})==2


def test_rename_invalidates_seal_and_cannot_stamp_old_issuer(contract_pdf_app):
    client, _, ids = contract_pdf_app
    _login(client,'p135a-admin')
    assert _seal(client,1).status_code==200
    old_pdf=_export(client,ids['own'],3,'rename-before-export-001')
    r=_edit(client,1,company_name='虚构更名包装有限公司')
    assert r.status_code==200,r.text
    assert not client.get('/api/system/company/seal/settings?company_id=1').json()['available']
    assert _seal(client,1).status_code==200
    assert _export(client,ids['own'],3,'rename-after-export-001').status_code==409
    # Legacy export receipts remain byte-exact even after company/seal changes.
    replay=client.post(f'/api/contracts/seal/{ids["own"]}/pdf',json=dict(expected_version=3,seal_version=1,company_id=1,company_version=0,operation_key='rename-before-export-001'))
    assert replay.content==old_pdf.content


def test_stale_company_and_seal_operations_do_not_write(contract_pdf_app):
    client, factory, _=contract_pdf_app
    _login(client,'p135a-admin')
    old=_settings(client)
    b=_create(client)
    assert client.post(f'/api/system/companies/{b["id"]}/activate',json=dict(expected_version=b['version'],selection_version=old['selection_version'])).status_code==409
    stale=client.get('/api/system/company/seal/settings?company_id=1').json()
    assert _edit(client,1,address='虚构地址修正').status_code==200
    r=client.post('/api/system/company/seal/settings?company_id=1',json=dict(company_version=stale['company_version'],expected_version=stale['version'],image_base64=base64.b64encode(_test_image()).decode()))
    assert r.status_code==409
    from app.models.contract_seal import ContractSeal
    with factory() as db: assert db.scalar(select(func.count()).select_from(ContractSeal))==0


@pytest.mark.parametrize('user',['p135a-scoped','p135a-denied'])
def test_company_management_requires_admin(contract_pdf_app,user):
    client,_,ids=contract_pdf_app
    _login(client,user)
    assert client.get('/api/system/company').status_code==403
    assert client.post('/api/system/companies',json=dict(company_name='虚构未授权公司',selection_version=0)).status_code==403
    assert client.put('/api/system/companies/1',json=dict(company_name='虚构未授权公司',expected_version=0,selection_version=0)).status_code==403
    assert client.post('/api/system/companies/1/activate',json=dict(expected_version=0,selection_version=0)).status_code==403
    assert client.get('/api/system/company/seal/settings?company_id=1').status_code==403
    assert client.get(f'/api/contracts/seal/{ids["own"]}/settings').status_code==403


def test_duplicate_and_audit_failure_leave_companies_unchanged(contract_pdf_app,monkeypatch):
    client,factory,_=contract_pdf_app
    _login(client,'p135a-admin')
    before=_settings(client)
    duplicate=client.post('/api/system/companies',json=dict(company_name=before['company_name'],selection_version=0))
    assert duplicate.status_code==409
    assert _settings(client)==before
    from app.api import companies
    def fail(*a,**kw):raise RuntimeError('injected company audit failure')
    monkeypatch.setattr(companies,'audit',fail)
    with pytest.raises(Exception,match='injected company audit failure'):_create(client)
    assert _settings(client)==before
    from app.models.company_profile import CompanyProfile,CompanySelection
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CompanyProfile))==0
        assert db.get(CompanySelection,1) is None


def test_get_has_no_initialization_writes(contract_pdf_app):
    client,factory,_=contract_pdf_app
    _login(client,'p135a-admin')
    assert len(_settings(client)['profiles'])==1
    assert client.get('/api/system/company/seal/settings?company_id=1').status_code==200
    from app.models.company_profile import CompanyProfile,CompanySelection
    with factory() as db:
        assert db.get(CompanySelection,1) is None
        assert db.scalar(select(func.count()).select_from(CompanyProfile))==0


def test_existing_legacy_seal_stays_with_original_company(contract_pdf_app):
    import hashlib
    from app.models.contract_seal import ContractSeal, ContractSealState
    from app.models.company_profile import CompanyProfile
    from app.models.user import User
    client,factory,_=contract_pdf_app
    with factory() as db:
        actor=db.scalar(select(User).where(User.username=='p135a-admin'))
        png=_test_image()
        seal=ContractSeal(image_png=png,sha256=hashlib.sha256(png).hexdigest(),width_px=400,height_px=400,size_mm=40,actor_id=actor.id)
        db.add(seal);db.flush();sid=seal.id
        db.add(ContractSealState(id=1,version=7,active_seal_id=sid));db.commit()
    _login(client,'p135a-admin')
    before=client.get('/api/system/company/seal/image?company_id=1&version=7')
    assert before.status_code==200 and before.content==png
    b=_create(client)
    assert _activate(client,b['id']).status_code==200
    assert client.get('/api/system/company/seal/image?company_id=1&version=7').content==png
    assert not client.get(f'/api/system/company/seal/settings?company_id={b["id"]}').json()['available']
    with factory() as db:
        assert db.get(CompanyProfile,1).active_seal_id==sid
        assert db.get(ContractSealState,1).active_seal_id==sid


def test_old_export_receipt_replays_without_new_company_parameters(contract_pdf_app):
    from app.models.contract_seal import ContractSealedExport
    client,factory,ids=contract_pdf_app
    _login(client,'p135a-admin');assert _seal(client,1).status_code==200
    pdf=_export(client,ids['own'],3,'historical-receipt-001')
    with factory() as db:
        receipt=db.scalar(select(ContractSealedExport))
        # Metadata-created fictional DB: emulate the immutable pre-upgrade receipt format.
        payload=json.loads(receipt.request_json)
        payload.pop('company_id');payload.pop('company_version')
        receipt.request_json=json.dumps(payload,sort_keys=True);db.commit()
    response=client.post(f'/api/contracts/seal/{ids["own"]}/pdf',json=dict(expected_version=3,seal_version=1,operation_key='historical-receipt-001'))
    assert response.status_code==200 and response.content==pdf.content


def test_seal_management_is_only_in_company_section():
    source=(Path(__file__).parents[1]/'static/index.html').read_text(encoding='utf8')
    contract=source[source.index("activePage === 'contracts'"):source.index('<!-- 公司信息 -->')]
    assert '>管理电子章<' not in contract
    company=source[source.index('<!-- 公司信息 -->'):source.index('<!-- 公司信息编辑弹层 -->')]
    assert '>管理电子章<' in company and '>新增公司抬头<' in company and '>管理开票抬头<' in company
