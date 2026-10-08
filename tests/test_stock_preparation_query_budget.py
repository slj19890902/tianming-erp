from fastapi.testclient import TestClient
from sqlalchemy import event, select
from stock_preparation_legacy_fixture import stock_replenishment_app, base_stock_replenishment_app
from test_stock_preparation_flow import setup
from app.api.stock_preparation import router
from app.models.stock_replenishment import StockReplenishmentOrderItem


def test_current_page_budget_does_not_expand_with_historical_items(stock_replenishment_app):
    app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        setup(client)
        with factory() as db:
            item=db.scalar(select(StockReplenishmentOrderItem));engine=db.get_bind()
            values={c.name:getattr(item,c.name) for c in item.__table__.columns if c.name!='id'}
            values.update(inventory_lot_id=None,stocked_quantity=0)
            db.add_all([StockReplenishmentOrderItem(**values) for _ in range(200)]);db.commit()
        statements=[]
        def record(conn,cursor,statement,parameters,context,many):statements.append(statement)
        event.listen(engine,'before_cursor_execute',record)
        try:
            response=client.get('/api/production/stock-preparation?workspace=true&state=waiting&page_size=1&page=100')
        finally:event.remove(engine,'before_cursor_execute',record)
        assert response.status_code==200,response.text
        assert response.json()['total']==201 and len(response.json()['items'])==1
        selects=[s for s in statements if s.lstrip().upper().startswith('SELECT')]
        assert len(selects)<=30,len(selects)
        assert not any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in statements)
