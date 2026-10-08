export type DrawingSaveOption = 'order_only' | 'save_to_product' | 'overwrite_product'
export type DrawingState = { generation: number; token: string; filename: string; digest: string; saveOption: DrawingSaveOption; busy: boolean; error: string }
export function newDrawingState(): DrawingState {
  return { generation: 0, token: '', filename: '', digest: '', saveOption: 'order_only', busy: false, error: '' }
}
export function invalidateDrawing(state: DrawingState) {
  state.generation++
  Object.assign(state, { token: '', filename: '', digest: '', saveOption: 'order_only', busy: false, error: '' })
}
export async function contentDigest(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
  return Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('')
}
export function validateDrawingFile(file: Pick<File, 'name' | 'size'>) {
  if (!/\.(png|jpe?g|webp|pdf)$/i.test(file.name)) throw new Error('图纸仅支持PNG、JPG、WEBP和PDF')
  if (file.size < 1 || file.size > 20 * 1024 * 1024) throw new Error('图纸不能为空，单文件不能超过20MB')
}
export async function uploadDrawing(state: DrawingState, file: File, upload: (file: File) => Promise<{ token: string }>, stillCurrent: () => boolean) {
  const generation = ++state.generation
  state.busy = true; state.error = ''
  try {
    validateDrawingFile(file)
    const digest = await contentDigest(file)
    if (state.generation !== generation || !stillCurrent()) return false
    const result = await upload(file)
    if (state.generation !== generation || !stillCurrent()) return false
    if (!/^[a-f0-9]{32}$/i.test(result.token)) throw new Error('图纸上传回读缺少有效身份，请重新上传')
    Object.assign(state, { token: result.token, filename: file.name, digest, saveOption: 'order_only' })
    return true
  } catch (error) {
    if (state.generation === generation && stillCurrent()) state.error = error instanceof Error ? error.message : '图纸上传失败，请重试'
    return false
  } finally {
    if (state.generation === generation) state.busy = false
  }
}
export function drawingFields(state: DrawingState, permissions: { edit: boolean; overwrite: boolean }) {
  if (state.busy) throw new Error('图纸仍在上传，请稍后保存')
  if (state.error) throw new Error(state.error)
  if (!state.token) return {}
  if (state.saveOption !== 'order_only' && !permissions.edit) throw new Error('无常用箱图纸编辑权限')
  if (state.saveOption === 'overwrite_product' && !permissions.overwrite) throw new Error('无常用箱图纸覆盖权限')
  return { temp_drawing_token: state.token, drawing_save_option: state.saveOption }
}
export function draftDrawingPath(state: DrawingState) {
  if (!/^[a-f0-9]{32}$/i.test(state.token)) throw new Error('请先上传图纸')
  const extension = state.filename.match(/\.(png|jpe?g|webp|pdf)$/i)?.[0].toLowerCase()
  if (!extension) throw new Error('图纸类型不支持')
  return `/api/orders/draft-drawing/${state.token}/content/preview${extension}`
}
export function trustedDrawingPath(path: string) {
  if (!/^\/api\/(?:orders\/(?:draft-drawing\/[a-f0-9]{32}\/content\/preview|items\/[1-9]\d*\/drawing\/content\/file)|master\/products\/drawings\/[1-9]\d*\/content\/original)\.(?:png|jpe?g|webp|pdf|bin)$/i.test(path)) throw new Error('图纸地址不属于受保护的业务接口')
  if (path.includes('/draft-drawing/') && path.endsWith('.bin')) throw new Error('临时图纸类型不支持')
  return path
}
export async function verifyDrawingContent(expected: string, content: Blob) {
  if (!expected || await contentDigest(content) !== expected) throw new Error('已保存图纸内容回读不一致，请核对已保存订单')
}
