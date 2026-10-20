import { exactMoney } from './exactMoney'
/** Reconciliation display only; never change the underlying numeric value. */
export function reconciliationNumber(value: unknown): string {
  return exactMoney(value)
}

export function reconciliationNumberCell({ cellValue }: { cellValue: unknown }): string {
  return reconciliationNumber(cellValue)
}
