import '../../../static/ui/exact-money.js'

type Money = { format(value: unknown): string; multiply(a: unknown, b: unknown): string; sum(values: unknown[]): string }
const core = (globalThis as unknown as { TmExactMoney: Money }).TmExactMoney
export const exactMoney = core.format
export const multiplyMoney = core.multiply
export const sumMoney = core.sum
