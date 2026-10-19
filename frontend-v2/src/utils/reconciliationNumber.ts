/** Reconciliation display only; never change the underlying numeric value. */
export function reconciliationNumber(value: unknown): string {
  const number = Number(value || 0)
  if (!Number.isFinite(number)) return '—'
  const rounded = Number(number.toLocaleString('en-US', {
    useGrouping: false, minimumFractionDigits: 3, maximumFractionDigits: 3,
  }))
  return rounded === 0 ? '0' : rounded.toLocaleString('en-US', {
    useGrouping: false, minimumFractionDigits: Number.isInteger(rounded) ? 0 : 3, maximumFractionDigits: 3,
  })
}

export function reconciliationNumberCell({ cellValue }: { cellValue: unknown }): string {
  return reconciliationNumber(cellValue)
}
