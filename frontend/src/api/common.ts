export class RequestError extends Error {
  constructor(message: string, readonly status: number, readonly code?: string) {
    super(message)
  }
}

export async function apiRequest<T>(path: string, init?: RequestInit, timeout = 60_000): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { Accept: 'application/json', ...init?.headers },
      signal: init?.signal ?? AbortSignal.timeout(timeout),
    })
  } catch {
    throw new RequestError('连接失败或请求超时，已保留当前输入。请重新读取状态后再决定是否重试。', 0)
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: { message?: string; code?: string } } | null
    throw new RequestError(payload?.detail?.message ?? `请求未完成（${response.status}）`, response.status, payload?.detail?.code)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const postJson = (value: unknown): RequestInit => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
})
export const withIdempotencyKey = (init: RequestInit, key: string): RequestInit => ({
  ...init,
  headers: { ...init.headers, 'Idempotency-Key': key },
})
export const putJson = (value: unknown): RequestInit => ({
  method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
})
export const patchJson = (value: unknown): RequestInit => ({
  method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
})
