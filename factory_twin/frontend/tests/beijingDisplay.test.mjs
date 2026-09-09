import {test} from 'node:test';
import assert from 'node:assert/strict';
import {beijingDisplay} from '../src/beijingDisplay.mjs';
test('Beijing minutes, independent of device timezone', () => {
  assert.equal(beijingDisplay('2026-09-09T02:05:21Z'), '2026-09-09 10:05');
  assert.equal(beijingDisplay('2026-09-09T16:05:21Z'), '2026-09-10 00:05');
  assert.equal(beijingDisplay('2026-09-09T10:05:21+08:00'), '2026-09-09 10:05');
  assert.equal(beijingDisplay('2026-09-09'), '2026-09-09');
  assert.equal(beijingDisplay(null), '—');
  assert.equal(beijingDisplay('2026-09-09T10:05:21'), '时间待确认');
  assert.equal(beijingDisplay('invalidZ'), '时间待确认');
});
