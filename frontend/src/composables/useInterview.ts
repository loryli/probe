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
  const preservedAnswer = ref<{ text: string; question: string } | null>(null)
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
  let recoveryDeadline = 0
  let recoveryKey = ''
  let readGeneration = 0
  let mutationGeneration = 0
  let disposed = false

  const sessionId = computed(() => typeof route.params.sessionId === 'string' ? route.params.sessionId : '')
  const currentQuestion = computed(() => session.value?.current_turn ?? null)
  const isLocked = computed(() => ['loading', 'saving', 'streaming', 'recovering', 'control', 'report'].includes(phase.value))
  const canAnswer = computed(() => session.value?.id === sessionId.value && session.value?.status === 'active' && !!currentQuestion.value && !isLocked.value)
  const availablePlans = computed(() => options.value?.plans.filter((plan) => plan.usable) ?? [])

  function confirmDiscard() {
    return (!answer.value.trim() && !preservedAnswer.value) || window.confirm('当前有未提交的回答草稿。离开会放弃草稿，仍要继续吗？')
  }

  function applySession(next: InterviewSession) {
    const previous = session.value
    if (previous?.id === next.id && next.revision < previous.revision) return
    if (previous?.id !== next.id) {
      answer.value = ''; preservedAnswer.value = null; report.value = null; liveText.value = ''
    } else if (previous?.current_turn && previous.current_turn.id !== next.current_turn?.id && answer.value.trim()) {
      const sequence = previous.current_turn.sequence
      const saved = next.turns.some((turn) => turn.role === 'candidate'
        && turn.sequence > sequence && turn.text === answer.value.trim())
      if (!saved) preservedAnswer.value = { text: answer.value, question: previous.current_turn.text }
      answer.value = ''
    }
    session.value = next
  }
  function restoreAnswer() {
    if (!preservedAnswer.value || !canAnswer.value) return
    if (answer.value.trim() && !window.confirm('用保留草稿替换当前编辑内容？')) return
    answer.value = preservedAnswer.value.text
    preservedAnswer.value = null
  }

  function closeStream() {
    stream.value?.close()
    stream.value = null
    if (recoveryTimer) window.clearTimeout(recoveryTimer)
  }

  async function loadSession(id: string, recover = false) {
    if (!id) return
    const token = ++readGeneration
    if (recover) phase.value = 'recovering'
    try {
      const next = await interviewApi.get(id)
      if (disposed || token !== readGeneration || sessionId.value !== id) return
      applySession(next)
      if (next.status === 'active' && !next.current_turn && next.pending_turn_id && !next.error_code) {
        phase.value = 'streaming'
        attachEvents(id)
      } else if (phase.value === 'recovering' || phase.value === 'loading') {
        phase.value = 'idle'
      }
      if (next.status === 'completed') await loadReport(id)
    } catch (cause) {
      if (token !== readGeneration || sessionId.value !== id) return
      error.value = cause instanceof Error ? cause.message : '读取本场失败。'
      if (phase.value === 'recovering') phase.value = 'idle'
    }
  }

  function attachEvents(id: string) {
    if (disposed || sessionId.value !== id) return
    closeStream()
    const key = `${id}:${session.value?.pending_turn_id ?? 'opening'}`
    if (recoveryKey !== key) { recoveryKey = key; recoveryDeadline = Date.now() + 360_000 }
    if (Date.now() >= recoveryDeadline) {
      phase.value = 'idle'
      error.value = '等待下一步已超过六分钟。回答已保存；请读取本场状态，确认后台调用结束后再重试。'
      return
    }
    const source = interviewApi.events(id)
    stream.value = source
    source.addEventListener('state', (event) => {
      if (stream.value !== source || sessionId.value !== id) return
      const next = JSON.parse((event as MessageEvent<string>).data) as InterviewSession
      applySession(next)
      if (next.current_turn || next.status !== 'active' || next.error_code) {
        closeStream()
        liveText.value = ''; notice.value = ''; recoveryKey = ''
        phase.value = 'idle'
        if (next.status === 'completed') void loadReport(id)
      }
    })
    source.addEventListener('error', (event) => {
      if (stream.value !== source || sessionId.value !== id) return
      closeStream()
      if (event instanceof MessageEvent && event.data) {
        const failure = JSON.parse(event.data) as { message?: string }
        error.value = failure.message || '读取场次状态失败。'
        phase.value = 'idle'
      } else {
        // 包括 CONNECTING；关闭浏览器的无限自动重连，先 GET 核对数据库状态。
        recoveryTimer = window.setTimeout(() => void loadSession(id, true), 1_500)
      }
    })
  }

  async function refresh() {
    if (!sessionId.value || ['saving', 'control', 'loading', 'report'].includes(phase.value)) return
    ++mutationGeneration
    closeStream(); recoveryKey = ''; error.value = ''
    phase.value = 'recovering'
    await loadSession(sessionId.value, true)
  }

  async function initialize() {
    pageBusy.value = true
    error.value = ''
    try {
      const [nextOptions, page] = await Promise.all([interviewApi.options(), interviewApi.list()])
      options.value = nextOptions
      mode.value = nextOptions.model.configured ? 'model' : 'rule'
      sessions.value = page.items
      const requestedPlan = typeof route.query.plan === 'string' ? route.query.plan : ''
      selectedPlan.value = availablePlans.value.some((plan) => plan.id === requestedPlan)
        ? requestedPlan
        : availablePlans.value[0]?.id ?? ''
      if (sessionId.value) {
        phase.value = 'loading'
        await loadSession(sessionId.value)
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '加载练习场失败。'
    } finally {
      pageBusy.value = false
    }
  }

  async function start() {
    if (!selectedPlan.value || pageBusy.value || isLocked.value) return
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
      const created = await interviewApi.create(selectedPlan.value, mode.value, requestId)
      if (disposed) return
      session.value = created
      pendingStart.value = null
      answer.value = ''
      await router.push(`/interviews/${session.value.id}`)
      sessions.value = (await interviewApi.list()).items
      if (!session.value.current_turn && session.value.status === 'active' && !session.value.error_code) {
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
    const token = ++mutationGeneration
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
          if (disposed || token !== mutationGeneration || sessionId.value !== active.id) return
          pendingAnswer.value = null
          session.value = ack
          answer.value = ''
          phase.value = 'streaming'
          notice.value = '回答已保存，面试官正在组织下一步。'
        },
        (delta) => { if (!disposed && token === mutationGeneration && sessionId.value === active.id) liveText.value += delta },
      )
      if (disposed || token !== mutationGeneration || sessionId.value !== active.id) return
      applySession(finalState)
      liveText.value = ''
      phase.value = 'idle'
      notice.value = ''
      if (finalState.status === 'completed') await loadReport(finalState.id)
      sessions.value = (await interviewApi.list()).items
    } catch (cause) {
      if (disposed || token !== mutationGeneration || sessionId.value !== active.id) return
      const message = cause instanceof ApiError ? cause.message : '连接中断，请读取本场状态。'
      error.value = message
      phase.value = 'recovering'
      await loadSession(active.id, true)
    }
  }

  async function retry() {
    if (!session.value || isLocked.value) return
    if (!session.value.error_code && !window.confirm('下一步状态尚未确认。继续生成可能调用模型；若原请求仍在运行，请先读取本场状态。确认继续？')) return
    const id = session.value.id
    const token = ++mutationGeneration
    phase.value = 'loading'
    error.value = ''
    try {
      const next = await interviewApi.retry(id)
      if (disposed || token !== mutationGeneration || sessionId.value !== id) return
      applySession(next)
      if (!session.value.current_turn && session.value.status === 'active' && !session.value.error_code) {
        phase.value = 'streaming'
        attachEvents(session.value.id)
      } else phase.value = 'idle'
    } catch (cause) {
      if (disposed || token !== mutationGeneration || sessionId.value !== id) return
      error.value = cause instanceof Error ? cause.message : '继续生成失败。'
      phase.value = 'idle'
    }
  }

  async function control(action: 'pause' | 'resume' | 'skip' | 'end') {
    if (!session.value || session.value.id !== sessionId.value || isLocked.value) return
    const labels = { pause: '暂停本场？', resume: '继续本场？', skip: '跳过当前考察目标并生成下一步？', end: '结束本场？已保存的问答会保留。' }
    if (session.value.phase === 'self_introduction' || session.value.phase === 'project_introduction') {
      labels.skip = '跳过当前介绍环节并进入下一阶段？未提交的介绍不会保存。'
    }
    if (['skip', 'end'].includes(action) && !window.confirm(labels[action])) return
    const active = session.value
    const token = ++mutationGeneration
    phase.value = 'control'
    error.value = ''
    try {
      const next = await interviewApi.control(active.id, action, active.revision)
      if (disposed || token !== mutationGeneration || sessionId.value !== active.id) return
      applySession(next)
      liveText.value = ''
      if (action === 'resume' && !session.value.current_turn && session.value.pending_turn_id && session.value.status === 'active' && !session.value.error_code) {
        phase.value = 'streaming'
        attachEvents(active.id)
      } else phase.value = 'idle'
      if (session.value.status === 'completed') await loadReport(active.id)
      sessions.value = (await interviewApi.list()).items
    } catch (cause) {
      if (disposed || token !== mutationGeneration || sessionId.value !== active.id) return
      error.value = cause instanceof Error ? cause.message : '控制操作未完成。'
      await loadSession(active.id, true)
    }
  }

  async function loadReport(id: string) {
    reportBusy.value = true
    try {
      const next = await interviewApi.report(id)
      if (session.value?.id === id) report.value = next
    } catch (cause) {
      if (session.value?.id !== id) return
      report.value = null
      if (cause instanceof ApiError && cause.status !== 404) error.value = cause.message
    } finally {
      if (session.value?.id === id) reportBusy.value = false
    }
  }

  async function generateReport() {
    if (!session.value || session.value.status !== 'completed' || reportBusy.value) return
    const id = session.value.id
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
      const next = await interviewApi.createReport(id, requestId)
      if (sessionId.value !== id) return
      report.value = next
      pendingReportRequest.value = null
    } catch (cause) {
      if (sessionId.value !== id) return
      error.value = cause instanceof Error ? cause.message : '复盘生成失败。'
    } finally {
      if (sessionId.value === id) { reportBusy.value = false; phase.value = 'idle' }
    }
  }

  function newSession() {
    if (pageBusy.value) return
    if (!confirmDiscard()) return
    closeStream()
    session.value = null
    report.value = null
    answer.value = ''
    preservedAnswer.value = null
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
        preservedAnswer.value = null
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
    if (!answer.value.trim() && !preservedAnswer.value) return
    event.preventDefault()
    event.returnValue = ''
  }
  window.addEventListener('beforeunload', protectDraft)

  watch(sessionId, async (id) => {
    ++mutationGeneration
    ++readGeneration
    closeStream(); recoveryKey = ''; reportBusy.value = false; liveText.value = ''
    if (id && id !== session.value?.id) {
      session.value = null; report.value = null; answer.value = ''; preservedAnswer.value = null
      await loadSession(id, true)
    }
    else if (!id) {
      closeStream()
      session.value = null
      report.value = null
      answer.value = ''; preservedAnswer.value = null
      phase.value = 'idle'
    }
  })
  onBeforeUnmount(() => {
    disposed = true; ++mutationGeneration; ++readGeneration
    closeStream()
    window.removeEventListener('beforeunload', protectDraft)
  })

  return {
    options, sessions, session, report, selectedPlan, mode, answer, preservedAnswer, restoreAnswer, liveText, phase,
    pageBusy, reportBusy, deleteBusy, checkedSessionIds, error, notice, sessionId,
    currentQuestion, isLocked, canAnswer,
    availablePlans, initialize, start, submitAnswer, retry, control, refresh,
    generateReport, loadSession, loadReport, newSession, deleteSessions,
  }
}
