const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')
const source = fs.readFileSync(path.join(__dirname, '../src/utils/reconciliationNumber.ts'), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
const exported = {}
new Function('exports', compiled)(exported)
test('new statement view shares the integer and three decimal display contract', () => {
  for (const [value, expected] of [['222.525', '222.525'], ['222.5', '222.500'], ['222.000', '222'], [0, '0'], [-0.0004, '0'], [-2.5, '-2.500'], [1.2345, '1.235'], [-1.2345, '-1.235'], [222.9996, '223']]) {
    assert.equal(exported.reconciliationNumber(value), expected)
    assert.equal(exported.reconciliationNumberCell({ cellValue: value }), expected)
  }
  const view = fs.readFileSync(path.join(__dirname, '../src/views/StatementView.vue'), 'utf8')
  assert.equal((view.match(/:formatter="reconciliationNumberCell"/g) || []).length, 5)
  assert.ok(view.includes('reconciliationNumber(pendingTotal.value)'))
  assert.ok(view.includes('reconciliationNumber(settlingStatement?.total_receivable)'))
})
