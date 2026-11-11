import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ApiError, interviewApi } from '../api/interviews'
import type { InterviewSession, SessionOptions, SessionReport, SessionSummary } from '../types'

export type InterviewPhase = 'idle' | 'loading' | 'answering' | 'saving' | 'streaming' | 'recovering' | 'control' | 'report'

export function useInterview() {
  const route = useRoute()
  const router = useRouter()
  const options = ref<SessionOptions | null>(null)
  const sessions = ref<SessionSummary[]>([])
  const session = ref<InterviewSession | null>(null)
  const report = ref<SessionReport | null>(null)
  const selectedPlan = ref('')
  const mode = ref<'model' | 'rule'>('model')
  const answer = ref('')
  const liveText = ref('')
  const phase = ref<InterviewPhase>('idle')
  const pageBusy = ref(false)
  const reportBusy = ref(false)
  const deleteBusy = ref(false)
  const checkedSessionIds = ref<string[]>([])
  const error = ref('')
  const notice = ref('')
  const stream = ref<EventSource | null>(null)
  const pendingStart = ref<{ signature: string; requestId: string } | null>(null)
  const pendingAnswer = ref<{ turnId: string; text: string; requestId: string } | null>(null)
  const pendingReportRequest = ref<{ sessionId: string; requestId: string } | null>(null)
  let recoveryTimer: number | undefined

  const sessionId = computed(() => typeof route.params.sessionId === 'string' ? route.params.sessionId : '')
  const currentQuestion = computed(() => session.value?.current_turn ?? null)
  const isLocked = computed(() => ['saving', 'streaming', 'recovering', 'control'].includes(phase.value))
  const canAnswer = computed(() => session.value?.status === 'active' && !!currentQuestion.value && !isLocked.value)
  const availablePlans = computed(() => options.value?.plans.filter((plan) => plan.usable) ?? [])

  function confirmDiscard() {
    return !answer.value.trim() || window.confirm('当前回答还没有提交。离开会放弃这段草稿，仍要继续吗？')
  }

  function closeStream() {
    stream.value?.close()
    stream.value = null
    if (recoveryTimer) window.clearTimeout(recoveryTimer)
  }

  async function loadSession(id: string, recover = false) {
    if (!id) return
    if (recover) phase.value = 'recovering'
    try {
      session.value = await interviewApi.get(id)
      if (session.value.current_turn) answer.value = ''
      if (session.value.status === 'active' && !session.value.current_turn && session.value.pending_turn_id) {
        phase.value = 'streaming'
        attachEvents(id)
      } else if (phase.value === 'recovering' || phase.value === 'loading') {
        phase.value = 'idle'
      }
      if (session.value.status === 'completed') await loadReport(id)
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '读取本场失败。'
      if (phase.value === 'recovering') phase.value = 'idle'
    }
  }

  function attachEvents(id: string) {
    closeStream()
    const source = interviewApi.events(id)
    stream.value = source
    source.addEventListener('state', (event) => {
      const next = JSON.parse((event as MessageEvent<string>).data) as InterviewSession
      session.value = next
      if (next.current_turn || next.status !== 'active' || next.error_code) {
        closeStream()
        phase.value = 'idle'
      }
    })
    source.addEventListener('error', () => {
      if (source.readyState === EventSource.CLOSED) {
        closeStream()
        recoveryTimer = window.setTimeout(() => void loadSession(id, true), 600)
      }
    })
  }

  async function refresh() {
    if (!sessionId.value) return
    phase.value = 'recovering'
    await loadSession(sessionId.value, true)
    if (session.value?.status === 'active' && !session.value.current_turn && session.value.pending_turn_id) {
      phase.value = 'streaming'
      attachEvents(sessionId.value)
    }
  }

  async function initialize() {
    pageBusy.value = true
    error.value = ''
    try {
      const [nextOptions, page] = await Promise.all([interviewApi.options(), interviewApi.list()])
      options.value = nextOptions
      sessions.value = page.items
      const requestedPlan = typeof route.query.plan === 'string' ? route.query.plan : ''
      selectedPlan.value = availablePlans.value.some((plan) => plan.id === requestedPlan)
        ? requestedPlan
        : availablePlans.value[0]?.id ?? ''
      if (sessionId.value) {
        phase.value = 'loading'
        await loadSession(sessionId.value)
        if (session.value?.status === 'completed') await loadReport(sessionId.value)
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '加载练习场失败。'
    } finally {
      pageBusy.value = false
    }
  }

  async function start() {
    if (!selectedPlan.value) return
    pageBusy.value = true
    error.value = ''
    notice.value = ''
    phase.value = 'loading'
    const signature = `${selectedPlan.value}:${mode.value}`
    let requestId = pendingStart.value?.signature === signature ? pendingStart.value.requestId : ''
    if (!requestId) {
      requestId = crypto.randomUUID()
      pendingStart.value = { signature, requestId }
    }
    try {
      session.value = await interviewApi.create(selectedPlan.value, mode.value, requestId)
      pendingStart.value = null
      answer.value = ''
      await router.push(`/interviews/${session.value.id}`)
      sessions.value = (await interviewApi.list()).items
      if (!session.value.current_turn && session.value.status === 'active') {
        phase.value = 'streaming'
        attachEvents(session.value.id)
      } else phase.value = 'idle'
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '无法开始练习。'
      phase.value = 'idle'
    } finally {
      pageBusy.value = false
    }
  }

  async function submitAnswer() {
    const active = session.value
    const text = answer.value.trim()
    if (!active || !active.current_turn || !text || !canAnswer.value) return
    error.value = ''
    notice.value = ''
    liveText.value = ''
    phase.value = 'saving'
    let requestId = pendingAnswer.value?.turnId === active.current_turn.id && pendingAnswer.value.text === text
      ? pendingAnswer.value.requestId
      : ''
    if (!requestId) {
      requestId = crypto.randomUUID()
      pendingAnswer.value = { turnId: active.current_turn.id, text, requestId }
    }
    try {
      const finalState = await interviewApi.submitAnswer(
        active.id,
        active.current_turn.id,
        text,
        requestId,
        (ack) => {
          pendingAnswer.value = null
          session.value = ack
          answer.value = ''
          phase.value = 'streaming'
          notice.value = '回答已保存，面试官正在组织下一步。'
        },
        (delta) => { liveText.value += delta },
      )
      session.value = finalState
      liveText.value = ''
      phase.value = 'idle'
      notice.value = ''
      if (finalState.status === 'completed') await loadReport(finalState.id)
      sessions.value = (await interviewApi.list()).items
    } catch (cause) {
      const message = cause instanceof ApiError ? cause.message : '连接中断，请读取本场状态。'
      error.value = message
      phase.value = 'recovering'
      await loadSession(active.id, true)
      if (session.value?.pending_turn_id && !session.value.current_turn && session.value.status === 'active') {
        attachEvents(active.id)
      } else phase.value = 'idle'
    }
  }

  async function retry() {
    if (!session.value) return
    phase.value = 'loading'
    error.value = ''
    try {
      session.value = await interviewApi.retry(session.value.id)
      if (!session.value.current_turn && session.value.status === 'active') {
        phase.value = 'streaming'
        attachEvents(session.value.id)
      } else phase.value = 'idle'
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '继续生成失败。'
      phase.value = 'idle'
    }
  }

  async function control(action: 'pause' | 'resume' | 'skip' | 'end') {
    if (!session.value || phase.value === 'control') return
    const labels = { pause: '暂停本场？', resume: '继续本场？', skip: '跳过当前考察目标并生成下一步？', end: '结束本场？已保存的问答会保留。' }
    if (['skip', 'end'].includes(action) && !window.confirm(labels[action])) return
    const active = session.value
    phase.value = 'control'
    error.value = ''
    try {
      session.value = await interviewApi.control(active.id, action, active.revision)
      liveText.value = ''
      if (action === 'resume' && !session.value.current_turn && session.value.status === 'active') {
        phase.value = 'streaming'
        attachEvents(active.id)
      } else phase.value = 'idle'
      if (session.value.status === 'completed') await loadReport(active.id)
      sessions.value = (await interviewApi.list()).items
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '控制操作未完成。'
      await loadSession(active.id, true)
      phase.value = 'idle'
    }
  }

  async function loadReport(id: string) {
    reportBusy.value = true
    try {
      report.value = await interviewApi.report(id)
    } catch (cause) {
      report.value = null
      if (cause instanceof ApiError && cause.status !== 404) error.value = cause.message
    } finally {
      reportBusy.value = false
    }
  }

  async function generateReport() {
    if (!session.value) return
    reportBusy.value = true
    phase.value = 'report'
    error.value = ''
    let requestId = pendingReportRequest.value?.sessionId === session.value.id
      ? pendingReportRequest.value.requestId
      : ''
    if (!requestId) {
      requestId = crypto.randomUUID()
      pendingReportRequest.value = { sessionId: session.value.id, requestId }
    }
    try {
      report.value = await interviewApi.createReport(session.value.id, requestId)
      pendingReportRequest.value = null
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '复盘生成失败。'
    } finally {
      reportBusy.value = false
      phase.value = 'idle'
    }
  }

  function newSession() {
    if (!confirmDiscard()) return
    closeStream()
    session.value = null
    report.value = null
    answer.value = ''
    liveText.value = ''
    error.value = ''
    notice.value = ''
    phase.value = 'idle'
    void router.push('/interviews')
  }

  async function deleteSessions(): Promise<boolean> {
    const ids = [...checkedSessionIds.value]
    if (!ids.length || deleteBusy.value) return false
    if (sessions.value.some((item) => ids.includes(item.id) && item.status === 'active')) {
      error.value = '进行中的场次需先结束或暂停，才能删除。'
      return false
    }
    const currentDeleted = !!session.value && ids.includes(session.value.id)
    const confirmed = window.confirm(
      `永久删除选中的 ${ids.length} 场练习记录？\n\n`
      + '全部问答、目标进度与整场复盘会一起删除，不可恢复；已确认的面试计划不受影响。'
      + (currentDeleted ? '当前打开的场次与未提交的回答草稿也会一并丢弃。' : ''),
    )
    if (!confirmed) return false
    deleteBusy.value = true
    error.value = ''
    notice.value = ''
    try {
      const result = await interviewApi.remove(ids)
      checkedSessionIds.value = []
      if (currentDeleted) {
        // 先清空草稿，避免路由守卫再次要求确认放弃。
        closeStream()
        answer.value = ''
        liveText.value = ''
        session.value = null
        report.value = null
        phase.value = 'idle'
        await router.push('/interviews')
      }
      sessions.value = (await interviewApi.list()).items
      notice.value = `已删除 ${result.deleted} 场练习记录。`
      return true
    } catch (cause) {
      error.value = cause instanceof ApiError ? cause.message : '删除记录失败，请刷新后重试。'
      return false
    } finally {
      deleteBusy.value = false
    }
  }

  onBeforeRouteUpdate((to, from) => {
    if (to.params.sessionId !== from.params.sessionId && !confirmDiscard()) return false
  })
  onBeforeRouteLeave(() => {
    if (!confirmDiscard()) return false
  })
  function protectDraft(event: BeforeUnloadEvent) {
    if (!answer.value.trim()) return
    event.preventDefault()
    event.returnValue = ''
  }
  window.addEventListener('beforeunload', protectDraft)

  watch(sessionId, async (id) => {
    if (id && id !== session.value?.id) await loadSession(id, true)
    else if (!id) {
      closeStream()
      session.value = null
      report.value = null
      phase.value = 'idle'
    }
  })
  onBeforeUnmount(() => {
    closeStream()
    window.removeEventListener('beforeunload', protectDraft)
  })

  return {
    options, sessions, session, report, selectedPlan, mode, answer, liveText, phase,
    pageBusy, reportBusy, deleteBusy, checkedSessionIds, error, notice, sessionId,
    currentQuestion, isLocked, canAnswer,
    availablePlans, initialize, start, submitAnswer, retry, control, refresh,
    generateReport, loadSession, loadReport, newSession, deleteSessions,
  }
}
