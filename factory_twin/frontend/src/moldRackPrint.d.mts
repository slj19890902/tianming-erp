export interface MoldPrintScope {floorCode: string; rackId: string; cellId?: string; level?: number; grid?: number}
export interface MoldPrintState {busy: boolean; pending: boolean; message?: string}
export function moldRackPrintSelection(response: unknown, scope: MoldPrintScope): Array<{id: number}>;
export function createMoldRackPrinter(options: {
  request: (path: string, body?: unknown) => Promise<any>;
  openWindow: (url: string) => Window | null;
  confirmReprint: (message: string) => boolean;
  changed: (state: MoldPrintState) => void;
  makeKey: () => string;
}): {run: (scope: MoldPrintScope, template?: string) => Promise<void>; retry: () => Promise<void> | undefined};
