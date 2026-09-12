export function mergeWarehouseSearchPage<T extends {items: Array<{lot_id: number}>; resources: Array<{resource_id: string}>}>(previous: T | null, page: T): T;
export function searchPageRequestIsCurrent(requestId: number, currentRequestId: number, aborted?: boolean): boolean;
