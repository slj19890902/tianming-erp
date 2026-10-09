/** One deadline covers headers AND body; cancelling never retries a mutation. */
export async function withRequestDeadline<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  timeoutError: () => Error,
  external?: AbortSignal | null,
  milliseconds = 30000,
): Promise<T> {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  let cancel = () => {}
  const deadline = new Promise<never>((_, reject) => {
    cancel = () => { controller.abort(); reject(new DOMException('请求已取消', 'AbortError')) }
    if (external?.aborted) { cancel(); return }
    external?.addEventListener('abort', cancel, { once: true })
    timer = setTimeout(() => { reject(timeoutError()); controller.abort() }, milliseconds)
  })
  try {
    return await Promise.race([deadline, Promise.resolve().then(() => {
      if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
      return operation(controller.signal)
    })])
  } finally {
    clearTimeout(timer)
    external?.removeEventListener('abort', cancel)
  }
}
