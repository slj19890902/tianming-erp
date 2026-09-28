export interface WarehouseWorkspaceActivation {
  url: string;
  pathname: "/warehouse.html" | "/warehouse-ledger.html";
  q?: string;
  floor?: string;
  locationId?: number | null;
  lotId?: number | null;
  tab?: string;
  action?: string;
}

export interface WarehouseWorkspaceGuardState {
  moveSubmitting: boolean;
  stocktakeSubmitting: boolean;
  mergeSubmitting: boolean;
  otherSubmitting: boolean;
  moveUncertain: boolean;
  stocktakeRefreshRequired: boolean;
  pendingRefreshRequired: boolean;
  rackOperationBlocked?: string;
}

export function normalizeWarehouseWorkspaceUrl(value: unknown, origin: string): string | null;
export function warehouseWorkspaceActivation(value: unknown, origin: string): WarehouseWorkspaceActivation | null;
export function warehouseWorkspaceBlockMessage(state: WarehouseWorkspaceGuardState): string;
