// API instants must carry an explicit offset; never infer the device timezone.
export function beijingDisplay(value) {
  if (!value) return '—';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  if (!/(Z|[+-]\d{2}:\d{2})$/i.test(value)) return '时间待确认';
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '时间待确认';
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}
