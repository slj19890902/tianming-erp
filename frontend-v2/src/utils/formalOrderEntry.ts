export type FormalOrderEntry = 'new' | 'import' | 'email'

export function parseFormalOrderEntry(action: unknown, requestId: unknown) {
  if (action !== 'new' && action !== 'import' && action !== 'email') return null
  if (typeof requestId !== 'string' || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(requestId)) return null
  return { action, requestId }
}

export function formalOrderEntryQuery(action: FormalOrderEntry) {
  return { order_entry: action, order_request: crypto.randomUUID() }
}

// Kept workspaces have separate window identities. Never pick another tab's
// frame by a shared HTML id or accept messages from an inactive workspace.
export function currentFormalFrame(document: Document, routePath: string) {
  return Array.from(document.querySelectorAll<HTMLIFrameElement>('iframe[data-formal-route]'))
    .find(frame => frame.dataset.formalRoute === routePath)?.contentWindow || null
}
