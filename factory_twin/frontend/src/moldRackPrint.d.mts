export interface MoldPrintScope {floorCode: string; rackId: string; cellId?: string; level?: number; grid?: number}
export interface MoldPrintState {busy: boolean; pending: boolean; message?: string}
export function createMoldRackPrinter(options: {
  openWindow: (url: string) => Window | null;
  changed: (state: MoldPrintState) => void;
}): {run: (scope: MoldPrintScope, template?: string) => Promise<void>; retry: () => Promise<void> | undefined};
