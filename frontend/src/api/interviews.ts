import type { InterviewSession, SessionOptions, SessionPage, SessionReport } from '../types'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit, timeout = 60_000): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { Accept: 'application/json', ...init?.headers },
      signal: init?.signal ?? AbortSignal.timeout(timeout),
    })
  } catch {
    throw new ApiError('连接失败或请求超时。请重新读取本场状态确认操作结果。', 0)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null) as {
      detail?: { message?: string; code?: string }
    } | null
    const fallback: Record<number, string> = {
      404: '场次或计划已不存在。',
      409: '本场状态已变化，请刷新后再操作。',
      422: '输入不符合要求，请检查后重试。',
      503: '服务暂不可用，请检查数据库与模型配置。',
    }
    throw new ApiError(
      body?.detail?.message ?? fallback[response.status] ?? '操作未完成，请稍后重试。',
      response.status,
      body?.detail?.code,
    )
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const interviewApi = {
  options: () => request<SessionOptions>('/interviews/options'),
  list: () => request<SessionPage>('/interviews?limit=20&offset=0'),
  remove: (ids: string[]) => request<{ deleted: number }>(
    '/interviews/batch-delete', json({ session_ids: ids }),
  ),
  get: (id: string) => request<InterviewSession>(`/interviews/${id}`),
  create: (planId: string, mode: 'model' | 'rule', requestId: string) => request<InterviewSession>(
    '/interviews', json({ request_id: requestId, plan_id: planId, mode }), 330_000,
  ),
  retry: (id: string) => request<InterviewSession>(`/interviews/${id}/retry`, { method: 'POST' }, 330_000),
  control: (id: string, action: 'pause' | 'resume' | 'skip' | 'end', revision: number) =>
    request<InterviewSession>(`/interviews/${id}/${action}`, json({ expected_revision: revision }), 330_000),
  report: (id: string) => request<SessionReport>(`/interviews/${id}/report`),
  createReport: (id: string, requestId: string) => request<SessionReport>(
    `/interviews/${id}/report`, json({ request_id: requestId }), 330_000,
  ),
  events: (id: string) => new EventSource(`/interviews/${id}/events`),
  submitAnswer: async (
    id: string,
    turnId: string,
    answer: string,
    requestId: string,
    onAck: (session: InterviewSession) => void,
    onDelta: (text: string) => void,
  ): Promise<InterviewSession> => {
    let response: Response
    try {
      response = await fetch(`/interviews/${id}/answers/stream`, {
        method: 'POST',
        headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request_id: requestId, turn_id: turnId, answer_text: answer,
        }),
        signal: AbortSignal.timeout(360_000),
      })
    } catch {
      throw new ApiError('连接中断。回答可能已经保存，请先读取本场状态。', 0)
    }
    if (!response.ok) {
      const body = await response.json().catch(() => null) as {
        detail?: { message?: string; code?: string }
      } | null
      throw new ApiError(body?.detail?.message ?? '回答未能提交。', response.status, body?.detail?.code)
    }
    if (!response.body) throw new ApiError('服务没有返回可读取的数据流。', 0)

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let eventName = 'message'
    let data: string[] = []
    let finalState: InterviewSession | undefined
    const dispatch = () => {
      if (!data.length) return
      const payload = data.join('\n')
      if (eventName === 'ack') onAck(JSON.parse(payload) as InterviewSession)
      else if (eventName === 'delta') {
        const chunk = JSON.parse(payload) as { text?: string }
        if (chunk.text) onDelta(chunk.text)
      } else if (eventName === 'state') {
        finalState = JSON.parse(payload) as InterviewSession
      } else if (eventName === 'error') {
        const error = JSON.parse(payload) as { message?: string; code?: string }
        throw new ApiError(error.message ?? '下一步生成失败。', 0, error.code)
      }
      eventName = 'message'
      data = []
    }
    try {
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value, { stream: !done })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const rawLine of lines) {
          const line = rawLine.replace(/\r$/, '')
          if (!line) dispatch()
          else if (line.startsWith('event:')) eventName = line.slice(6).trim()
          else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
        }
        if (done) break
      }
      if (buffer) {
        if (buffer.startsWith('data:')) data.push(buffer.slice(5).trimStart())
      }
      // EOF 也提交已完整收到的最后一帧，不要求供应商额外发送空行。
      dispatch()
    } finally {
      await reader.cancel().catch(() => undefined)
      reader.releaseLock()
    }
    if (!finalState) throw new ApiError('回答已提交，但下一步状态尚未确认。请刷新本场。', 0, 'state_missing')
    return finalState
  },
}
