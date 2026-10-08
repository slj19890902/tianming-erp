const chinaDate = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
})

export function businessDate(date = new Date()): string {
  return chinaDate.format(date)
}

export function businessMonth(date = new Date()): string {
  return businessDate(date).slice(0, 7)
}
