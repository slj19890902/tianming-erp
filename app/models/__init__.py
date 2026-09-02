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
from app.models.ui_layout_revision import UiLayoutRevision  # noqa: E402,F401
from app.models.customer import Customer  # noqa: E402,F401
from app.models.customer_finished_storage_preference import (  # noqa: E402,F401
    CustomerFinishedStoragePreference,
)
from app.models.customer_quote_preference import CustomerQuotePreference  # noqa: E402,F401
from app.models.customer_material import (  # noqa: E402,F401
    CustomerMaterialCandidate,
    CustomerMaterialSelectionHistory,
)
from app.models.material import Material  # noqa: E402,F401
from app.models.supplier import (  # noqa: E402,F401
    ExternalPackagingProduct,
    Supplier,
    SupplierAlias,
    SupplierSupplyCategory,
)
from app.models.master_data_object_version import (  # noqa: E402,F401
    MasterDataObjectVersion,
)
from app.models.mold_tool import (  # noqa: E402,F401
    MoldLabelLayoutRevision,
    MoldLabelPrintJob,
    MoldLabelPrintJobItem,
    MoldLocationMovement,
    MoldMasterMutation,
    MoldRepairEvent,
    MoldScanEvent,
    MoldTool,
    MoldToolCustomer,
)
from app.models.printing_plate import (  # noqa: E402,F401
    PrintingPlate,
    PrintingPlateLocationMovement,
    PrintingPlateResinReuse,
)
from app.models.product import Product  # noqa: E402,F401
from app.models.product_bom import (  # noqa: E402,F401
    BomComponentDirectDeliveryAllocation,
    ProductBomComponent,
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.product_drawing import ProductDrawing  # noqa: E402,F401
from app.models.historical_requisition import (  # noqa: E402,F401
    HistoricalRequisitionMap,
)
from app.models.historical_purchase import HistoricalPurchaseEntry  # noqa: E402,F401
from app.models.migration import MigrationEntityMap  # noqa: E402,F401
from app.models.order import Order, OrderDailySequence, OrderItem  # noqa: E402,F401
from app.models.customer_charge import CustomerCharge  # noqa: E402,F401
from app.models.order_material_cost_snapshot import (  # noqa: E402,F401
    SalesOrderItemMaterialCostSnapshot,
)
from app.models.order_estimated_cost_snapshot import (  # noqa: E402,F401
    SalesOrderItemEstimatedCostSnapshot,
)
from app.models.requisition import (  # noqa: E402,F401
    Requisition,
    RequisitionDailySequence,
    RequisitionHold,
    RequisitionItem,
)
from app.models.delivery import (  # noqa: E402,F401
    Delivery,
    DeliveryDailySequence,
    DeliveryItem,
    DeliveryPickTask,
    DeliveryPickTaskItem,
)
from app.models.material_mapping import MaterialCodeMappingCandidate  # noqa: E402,F401
from app.models.material_price_history import (  # noqa: E402,F401
    MaterialPriceAdjustmentBatch,
    MaterialPriceHistory,
)
from app.models.external_packaging_price import (  # noqa: E402,F401
    ExternalPackagingPriceVersion,
)
from app.models.external_packaging_component import (  # noqa: E402,F401
    ProductExternalComponent,
    ProductExternalComponentCandidate,
    ProductExternalComponentSet,
)
from app.models.order_external_packaging import (  # noqa: E402,F401
    SalesOrderItemExternalComponent,
    SalesOrderItemExternalComponentCandidate,
)
from app.models.external_packaging_purchase import (  # noqa: E402,F401
    ExternalPackagingPurchaseBatch,
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseDailySequence,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingPurchasePurgeAuthorization,
    ExternalPackagingReceipt,
    ExternalPackagingReceiptItem,
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
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.stock_replenishment import (  # noqa: E402,F401
    InventoryStockPolicy,
    StockReplenishmentBomComponentPlan,
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.stocktake import (  # noqa: E402,F401
    StocktakeItem,
    StocktakeOrder,
    StocktakeReview,
)
from app.models.inventory_onboarding import (  # noqa: E402,F401
    InventoryOnboardingBatch,
    InventoryOnboardingLine,
)
from app.models.inventory_onboarding_posting import (  # noqa: E402,F401
    InventoryOnboardingPosting,
)
from app.models.finance import (  # noqa: E402,F401
    FinanceIdempotencyRecord,
    FinanceManualMutation,
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementAdjustment,
    StatementItem,
    StatementMonthlySequence,
)
from app.models.finance_payable import FinancePayable  # noqa: E402,F401
from app.models.supplier_settlement import (  # noqa: E402,F401
    SupplierReceiptSettlementPriceFact,
    SupplierMonthlyAdjustment,
    SupplierCreditLot,
    SupplierMonthlyInvoice,
    SupplierMonthlyPayment,
    SupplierPaymentBatch,
    SupplierMonthlyStatement,
    SupplierMonthlyStatementLine,
)
from app.models.finance_cost import (  # noqa: E402,F401
    FinanceCostCenter,
    FinanceCostPoolEntry,
)
from app.models.finance_simplified import (  # noqa: E402,F401
    FinanceAcceptanceNote,
    FinanceRecurringRule,
    FinanceUtilityExpense,
    FinanceUtilityMonthMode,
    FinanceUtilityReading,
)
from app.models.processing_cost import (  # noqa: E402,F401
    ProcessingCostSettings,
    ProductProcessingProfile,
)
from app.models.material_cost import (  # noqa: E402,F401
    FinanceDeliveryMaterialCostFact,
)
from app.models.fulfillment_reminder import (  # noqa: E402,F401
    FulfillmentReminder,
    FulfillmentReminderMutation,
)
from app.models.invoice_task import (  # noqa: E402,F401
    CustomerInvoiceItemRule,
    CustomerInvoiceProfile,
    CustomerInvoiceSellerChange,
    FinanceSettlementEntity,
    FinanceInvoiceAttachment,
    FinanceInvoiceTask,
    FinanceInvoiceTaskItem,
    InvoiceSellerEntity,
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
from app.models.customer_contract import (  # noqa: E402,F401
    ContractDailySequence,
    CustomerContract,
    CustomerContractItem,
)
from app.models.production import (  # noqa: E402,F401
    ProductionCompletion,
    ProductionCompletionBatch,
    ProductionStockTransfer,
    ProductionTask,
)
from app.models.production_label_print import (  # noqa: E402,F401
    ProductionLabelPlanRefresh,
    ProductionPackagingLabelLayoutRevision,
    ProductionPackagingLabelPrintJob,
    ProductionPackagingLabelPrintJobTask,
)
from app.models.warehouse_inventory import (  # noqa: E402,F401
    DeliveryInventoryAllocation,
    Floor3LocationLayout,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryLocationMovement,
    InventoryMovement,
    InventoryLotTransfer,
    WarehouseLocationDiscrepancy,
    WarehouseUnmatchedInventoryObservation,
    InventoryPallet,
    InventoryPalletItem,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseGroundPlacementMutation,
    InventoryReservation,
    OrderedFinishedReceiptReturn,
    OrderItemSemiRequirement,
    SemiFinishedInventoryDetail,
    SemiFinishedLotAllowedProduct,
    SemiFinishedMatchRule,
    SemiFinishedMatchRuleProduct,
    UnorderedFinishedDeliveryAllocation,
    UnorderedFinishedDeliveryReversal,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseLocation,
    WarehouseLocationAddressMutation,
    WarehouseLocationAlias,
    WarehouseRackLevelLabelPrintJob,
)
from app.models.incoming_receipt import (  # noqa: E402,F401
    IncomingReceipt,
    IncomingReceiptItem,
)
from app.models.purchase_receipt import (  # noqa: E402,F401
    IncomingReceiptBatchFact,
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
    IncomingReceiptReversalFact,
    ProductionCompletionReserveConversion,
    ProductionCompletionReserveConversionReversal,
    PurchaseReceiptFact,
    PurchaseReceiptMaterialVariance,
    PurchaseReceiptMaterialVarianceApproval,
)
from app.models.warehouse_capacity import (  # noqa: E402,F401
    WarehouseCapacityForecastPlan,
)
