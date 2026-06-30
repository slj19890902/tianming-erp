from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from app.models.user import User  # noqa: E402,F401
from app.models.audit import OperationLog  # noqa: E402,F401
from app.models.customer import Customer  # noqa: E402,F401
from app.models.material import Material  # noqa: E402,F401
from app.models.product import Product  # noqa: E402,F401
from app.models.product_drawing import ProductDrawing  # noqa: E402,F401
from app.models.historical_requisition import (  # noqa: E402,F401
    HistoricalRequisitionMap,
)
from app.models.migration import MigrationEntityMap  # noqa: E402,F401
from app.models.order import Order, OrderDailySequence, OrderItem  # noqa: E402,F401
from app.models.requisition import (  # noqa: E402,F401
    Requisition,
    RequisitionDailySequence,
    RequisitionItem,
)
from app.models.delivery import (  # noqa: E402,F401
    Delivery,
    DeliveryDailySequence,
    DeliveryItem,
)
from app.models.material_mapping import MaterialCodeMappingCandidate  # noqa: E402,F401
from app.models.material_price_history import (  # noqa: E402,F401
    MaterialPriceAdjustmentBatch,
    MaterialPriceHistory,
)
from app.models.supplier_flute_price_rule import (  # noqa: E402,F401
    SupplierFlutePriceRule,
)
from app.models.pdf_training import (  # noqa: E402,F401
    PdfOrderTrainingBatch,
    PdfOrderTrainingSample,
    PdfOrderCustomerTemplate,
    PdfOrderCorrectionLog,
)
from app.models.supplier_requisition_order import (  # noqa: E402,F401
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.finance import (  # noqa: E402,F401
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementItem,
    StatementMonthlySequence,
)
from app.models.company_config import CompanyConfig  # noqa: E402,F401
from app.models.tianhua_pre_delivery import (  # noqa: E402,F401
    TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem,
    TianhuaPreDeliveryImportBatch,
    TianhuaPreDeliveryImportItem,
)
from app.models.quotation import (  # noqa: E402,F401
    QuotationDailySequence,
    QuotationItem,
    QuotationOrder,
)
