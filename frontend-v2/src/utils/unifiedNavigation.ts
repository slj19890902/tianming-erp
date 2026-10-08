export interface NavigationItem { key: string; label: string; active: boolean; more: boolean }
export interface ShellUi {
  label: string; flow: boolean; items: NavigationItem[]; actorId: number; generation: number
  name: string; username: string; role: string; uiMode: 'standard' | 'large'; uiModeSaving: boolean
  canApprove: boolean; approvalLabel: string; busy: boolean
  overview: null | { floor: string; lots: number; occupied: number; locations: number; unlocated: number; conflicts: number }
}

export function navigationRequestId(): string {
  // The formal ERP is served over private-LAN HTTP, where randomUUID may be
  // unavailable. getRandomValues is also supported in that browser context.
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 15) | 64
  bytes[8] = (bytes[8]! & 63) | 128
  const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

// Only render the active, authenticated workspace's bounded projection. The
// original workspace remains the authority for permissions and business actions.
export function parseShellUi(value: unknown, actorId: number | undefined): ShellUi | null {
  if (!value || typeof value !== 'object') return null
  const data = value as ShellUi
  if (!Number.isSafeInteger(data.actorId) || data.actorId !== actorId || !Number.isSafeInteger(data.generation)) return null
  if (!['label', 'name', 'username', 'role', 'approvalLabel'].every(key =>
    typeof (data as unknown as Record<string, unknown>)[key] === 'string' && String((data as unknown as Record<string, unknown>)[key]).length <= 120)) return null
  if (!Array.isArray(data.items) || data.items.length > 20 || !data.items.every(item =>
    item && typeof item.key === 'string' && /^[a-z0-9_:-]{1,80}$/i.test(item.key)
    && typeof item.label === 'string' && item.label.length <= 40
    && typeof item.active === 'boolean' && typeof item.more === 'boolean')) return null
  if (!['standard', 'large'].includes(data.uiMode)) return null
  if (!['flow', 'uiModeSaving', 'canApprove', 'busy'].every(key => typeof (data as unknown as Record<string, unknown>)[key] === 'boolean')) return null
  if (data.overview && (typeof data.overview.floor !== 'string' || data.overview.floor.length > 16 ||
    !['lots', 'occupied', 'locations', 'unlocated', 'conflicts'].every(key => {
      const n = data.overview![key as 'lots']; return Number.isFinite(n) && n >= 0
    }))) return null
  return data
}
