const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const ts = require('typescript')

const source = fs.readFileSync(path.join(__dirname, '../src/utils/businessDate.ts'), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText
const moduleExports = {}
new Function('exports', compiled)(moduleExports)
const { businessDate, businessMonth } = moduleExports

test('Beijing date does not inherit the prior UTC date after midnight', () => {
  assert.equal(businessDate(new Date('2026-10-01T16:30:00Z')), '2026-10-02')
  assert.equal(businessMonth(new Date('2026-10-01T16:30:00Z')), '2026-10')
})

test('Beijing date crosses month and year at the correct local boundary', () => {
  assert.equal(businessDate(new Date('2026-09-30T16:30:00Z')), '2026-10-01')
  assert.equal(businessDate(new Date('2026-12-31T16:30:00Z')), '2027-01-01')
  assert.equal(businessMonth(new Date('2026-12-31T16:30:00Z')), '2027-01')
})
