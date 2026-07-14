from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from app.models.user import User  # noqa: E402,F401
from app.models.access_control import (  # noqa: E402,F401
    UserCustomerScope,
    UserPermissionOverride,
)
from app.models.audit import OperationLog  # noqa: E402,F401
from app.models.customer import Customer  # noqa: E402,F401
from app.models.material import Material  # noqa: E402,F401
from app.models.mold_tool import MoldTool  # noqa: E402,F401
from app.models.product import Product  # noqa: E402,F401
from app.models.product_drawing import ProductDrawing  # noqa: E402,F401
from app.models.historical_requisition import (  # noqa: E402,F401
    HistoricalRequisitionMap,
)
from app.models.historical_purchase import HistoricalPurchaseEntry  # noqa: E402,F401
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
from app.models.supplier_paper_code import SupplierPaperCode  # noqa: E402,F401
from app.models.supplier_material_rule import (  # noqa: E402,F401
    SupplierMaterialBasePrice,
    SupplierMaterialRuleConfig,
    SupplierMaterialSubstitutionRule,
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
from app.models.stock_replenishment import (  # noqa: E402,F401
    InventoryStockPolicy,
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
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
from app.models.warehouse_inventory import (  # noqa: E402,F401
    DeliveryInventoryAllocation,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedMatchRule,
    SemiFinishedMatchRuleProduct,
    WarehouseLocation,
)
from app.models.incoming_receipt import (  # noqa: E402,F401
    IncomingReceipt,
    IncomingReceiptItem,
)
