from fastapi.testclient import TestClient
from sqlalchemy import select,func
from app.api.email_intake import router
from app.models.email_intake import EmailIntakeAttachment,EmailIntakeDraft,EmailIntakeMessage
from app.models.order import Order
from tests.test_phase16_pdf_order_import import _order_import_app
from tests.test_email_order_link import session
from tests.test_email_excel import workbook
import hashlib


def test_excel_snapshot_grouping_replay_restore_and_scope(tmp_path):
    app=_order_import_app(tmp_path);app.include_router(router,prefix='/api/email-intake')
    content=workbook()
    with session(app) as db:
        row=EmailIntakeMessage(mailbox_key='fixture',uid_validity='1',uid=1);db.add(row);db.flush()
        db.add(EmailIntakeAttachment(message_id=row.id,part_number=1,filename='orders.xlsx',
            sha256=hashlib.sha256(content).hexdigest(),content=content));db.commit()
    with TestClient(app) as client:
        client.post('/api/auth/login',json={'username':'admin','password':'RolePass123!'})
        response=client.get('/api/email-intake/attachments/1/workbook?header_row=2')
        assert response.status_code==200,response.text
        data=response.json();assert 'rows' not in data
        payload={'customer_id':1,'sheet_name':data['sheet_name'],'header_row':2,'columns':data['columns']}
        response=client.post('/api/email-intake/attachments/1/excel-drafts',json=payload)
        assert response.status_code==200,response.text
        ids=response.json()['draft_ids'];assert len(ids)==2
        assert client.post('/api/email-intake/attachments/1/excel-drafts',json=payload).json()['draft_ids']==ids
        draft=client.get('/api/email-intake/drafts/'+str(ids[0]))
        assert draft.status_code==200,draft.text
        assert draft.json()['items'][0]['quantity']==20
        assert draft.json()['email_attachment_id']==1
        assert draft.json()['preview_safety_token']
        assert len(client.get('/api/email-intake/1').json()['attachments'][0]['drafts'])==2
        client.post('/api/auth/login',json={'username':'sales','password':'RolePass123!'})
        assert client.get('/api/email-intake/drafts/'+str(ids[0])).status_code==403
    with session(app) as db:
        assert db.scalar(select(func.count()).select_from(EmailIntakeDraft))==2
        assert db.scalar(select(func.count()).select_from(Order))==0
