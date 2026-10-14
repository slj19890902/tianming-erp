import type { Rack } from './types';
export interface MoldCellAliasPreview {
  id: string | null;
  level: number;
  grid: number;
  alias: string;
  status: 'current' | 'restore' | 'ambiguous' | 'new';
  historyAliases: string[];
  reused: boolean;
}
export function previewMoldCellAliases(rack: Rack | null): {cells: MoldCellAliasPreview[]; errors: string[]};
