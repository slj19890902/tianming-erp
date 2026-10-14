import test from 'node:test';
import assert from 'node:assert/strict';
import {previewMoldCellAliases} from '../src/moldCellAliases.mjs';

const cells = [1,2,3].map(grid => ({id: `id-${grid}`, level: 1, grid, alias: `C${grid}`}));
const rack = {mold_rack_code:'C', levels:1, level_cell_counts:[3], mold_cells:cells};

test('shrink filters removed aliases; expand previews unique original identities without client IDs', () => {
  const reduced = {...rack, level_cell_counts:[1]};
  assert.deepEqual(previewMoldCellAliases(reduced).cells.map(c => c.alias), ['C1']);
  const expanded = {...rack, mold_cells:cells.slice(0,1), retired_mold_cells:cells.slice(1)};
  const before = structuredClone(expanded);
  const preview = previewMoldCellAliases(expanded);
  assert.deepEqual(preview.cells.map(c => c.alias), ['C1','C2','C3']);
  assert.deepEqual(preview.cells.map(c => c.status), ['current','restore','restore']);
  assert.equal(preview.cells[1].id, null);
  assert.deepEqual(preview.cells[1].historyAliases, ['C2']);
  assert.deepEqual(expanded, before);
});

test('different position or ambiguous history gets a new cell and warns about reused short name', () => {
  const changed = {...rack, levels:2, level_cell_counts:[1,2], mold_cells:cells.slice(0,1), retired_mold_cells:cells.slice(1)};
  const preview = previewMoldCellAliases(changed);
  assert.deepEqual(preview.cells.map(c => c.alias), ['C1','C2','C3']);
  assert.equal(preview.cells[1].status, 'new');
  assert.equal(preview.cells[1].reused, true);
  const ambiguous = previewMoldCellAliases({...rack, mold_cells:cells.slice(0,1), retired_mold_cells:[cells[1], {...cells[1], id:'other-id', alias:'C4'}]});
  assert.equal(ambiguous.cells[1].status, 'ambiguous');
  assert.deepEqual(ambiguous.cells[1].historyAliases, ['C2','C4']);
});

test('active aliases may swap or reuse historical labels but duplicate and malformed current aliases block saving', () => {
  const swapped = {...rack, mold_cells:cells.map((c,i) => ({...c, alias:['C1','C3','C2'][i]})), retired_mold_cells:cells.slice(1)};
  assert.deepEqual(previewMoldCellAliases(swapped).errors, []);
  assert.match(previewMoldCellAliases({...rack, mold_cells:cells.map(c => ({...c, alias:'C1'}))}).errors[0], /重复/);
  assert.match(previewMoldCellAliases({...rack, mold_cells:[{...cells[0],alias:'C0'}]}).errors[0], /正整数/);
  assert.match(previewMoldCellAliases({...rack, level_cell_counts:[0]}).errors[0], /至少/);
  assert.deepEqual(previewMoldCellAliases({...rack, mold_rack_code:'R01'}), {cells:[], errors:[]});
});
