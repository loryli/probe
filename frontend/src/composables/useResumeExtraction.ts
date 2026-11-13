import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { RequestError, apiRequest, postJson } from '../api/common'

export interface ParseDraft {
  id: string
  revision: number
  source_version: number
  status: string
  provider: string
  model: string
  elapsed_ms: number | null
  created_at: string
  expires_at: string
  error_code: string | null
  error_message: string | null
  confirmed_projects: Record<string, string>
  result: {
    needs_confirmation: string[]
    projects: Array<{
      name: string; summary?: string | null; role?: string | null; duration?: string | null
      tech_stack: string[]; evidence_quote: string; confidence: number; needs_confirmation: string[]
      facts: Array<{ fact_type: string; content: string; evidence_quote: string; needs_confirmation?: string[] }>
    }>
  } | null
}

// 数据库草稿是唯一任务记录；只轮询 GET，网络错误不重发收费请求。
export function useResumeExtraction(resumeId: Ref<string>, version: Ref<number>) {
  const parseDraft = ref<ParseDraft | null>(null)
  const parseHistory = ref<ParseDraft[]>([])
  const parseBusy = ref(false)
  const backupBusy = ref(false)
  const parseLoading = ref(false)
  const parseError = ref('')
  const parseNotice = ref('')
  const parseElapsed = ref(0)
  const parseStage = ref('')
  const pendingRequest = ref('')
  const submissionMissing = ref(false)
  const parsePending = computed(() => !!pendingRequest.value || parseDraft.value?.status === 'running')
  let generation = 0
  let clock: number | undefined
  let backupRequest = ''
  let historyRequest: Promise<boolean> | null = null
  let historyLoaded = false

  function stopParseWatch() {
    ++generation
    if (clock !== undefined) window.clearInterval(clock)
    clock = undefined
    parseBusy.value = false
    backupBusy.value = false
  }
  function applyParseDraft(draft: ParseDraft) {
    parseDraft.value = draft
    submissionMissing.value = false
    // 备选候选和 AI 任务各自保留；切到本地规则不能丢掉仍在后台的 AI 请求标识。
    if (draft.provider !== 'local_rules') pendingRequest.value = draft.status === 'running' ? draft.id : ''
    parseHistory.value = [draft, ...parseHistory.value.filter((item) => item.id !== draft.id)]
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
    parseError.value = draft.status === 'failed' ? draft.error_message || '项目提取失败。' : ''
  }
  function stopWaiting() {
    stopParseWatch()
    parseNotice.value = '已停止页面等待，后台任务继续。可读取原任务；返回本页也能恢复记录。'
  }
  async function observe(draft: ParseDraft, token: number, id: string) {
    applyParseDraft(draft)
    const started = Date.parse(draft.created_at)
    const tick = () => { parseElapsed.value = Math.max(0, Math.floor((Date.now() - started) / 1000)) }
    tick()
    clock = window.setInterval(tick, 1000)
    // 服务端到期后会把 running 改为 failed；给最后一次读取留出余量。
    const expires = Date.parse(draft.expires_at)
    const deadline = Number.isFinite(expires) ? Math.max(Date.now() + 15_000, expires + 20_000) : Date.now() + 350_000
    let failures = 0
    while (draft.status === 'running' && token === generation && Date.now() < deadline) {
      parseStage.value = failures ? '状态读取暂时失败，正在重连原任务…' : '后台正在提取项目与原文依据…'
      await new Promise<void>((resolve) => window.setTimeout(resolve, 2_000))
      if (token !== generation) return
      try {
        draft = await apiRequest<ParseDraft>(`/materials/resumes/${id}/parse-drafts/${draft.id}`, undefined, 15_000)
        if (token !== generation) return
        applyParseDraft(draft)
        failures = 0
      } catch (cause) {
        if (token !== generation) return
        if (cause instanceof RequestError && [403, 404, 422].includes(cause.status)) throw cause
        if (++failures >= 3) throw new Error('连续三次未能读取任务。已停止等待，请检查后端服务，再读取原任务；不会重新调用模型。')
      }
    }
    if (token === generation && draft.status === 'running') {
      parseNotice.value = '任务尚未确认完成，已停止等待。请读取原任务确认结果，再决定是否重新提取。'
    }
  }
  async function readParseDraft(draftId: string) {
    if (!resumeId.value || parseBusy.value || parseLoading.value) return false
    stopParseWatch()
    const token = generation
    const id = resumeId.value
    parseBusy.value = true; parseError.value = ''; parseNotice.value = ''; parseStage.value = '正在读取原任务…'
    try {
      const draft = await apiRequest<ParseDraft>(`/materials/resumes/${id}/parse-drafts/${draftId}`)
      if (token === generation) {
        await observe(draft, token, id)
        return token === generation && ['ready', 'confirmed'].includes(parseDraft.value?.status ?? '')
      }
    } catch (cause) {
      if (token === generation) {
        parseError.value = cause instanceof Error ? cause.message : '读取提取记录失败。'
        if (cause instanceof RequestError && cause.status === 404 && pendingRequest.value === draftId) {
          submissionMissing.value = true
          parseNotice.value = '尚未找到原请求记录。可重试提交原请求，沿用同一标识，避免重复调用。'
        }
      }
    } finally { if (token === generation) stopParseWatch() }
    return false
  }
  function loadParseHistory(): Promise<boolean> {
    if (historyRequest) return historyRequest
    if (!resumeId.value || parseBusy.value) return Promise.resolve(false)
    const token = generation
    const id = resumeId.value
    const sourceVersion = version.value
    parseLoading.value = true
    const request = (async () => {
      try {
        const page = await apiRequest<{ items: ParseDraft[] }>(`/materials/resumes/${id}/parse-drafts?limit=20&source_version=${sourceVersion}`)
        if (token !== generation) return false
        parseHistory.value = page.items
        if (page.items[0]) applyParseDraft(page.items[0])
        const runningModel = page.items.find((item) => item.provider !== 'local_rules' && item.status === 'running')
        if (runningModel) pendingRequest.value = runningModel.id
        parseError.value = page.items[0]?.status === 'failed' ? page.items[0].error_message || '项目提取失败。' : ''
        historyLoaded = true
        return true
      } catch (cause) {
        if (token === generation) parseError.value = cause instanceof Error ? cause.message : '读取提取历史失败。'
        return false
      } finally { if (token === generation) parseLoading.value = false }
    })()
    historyRequest = request
    void request.then(() => { if (historyRequest === request) historyRequest = null })
    return request
  }
  async function runExtraction() {
    if (!resumeId.value || parseBusy.value) return false
    const source = `${resumeId.value}:${version.value}`
    // 保存简历会触发历史读取；明确的提取动作等待它完成，不能静默丢弃点击。
    if (parseLoading.value || !historyLoaded) {
      if (!await loadParseHistory()) {
        if (source === `${resumeId.value}:${version.value}`) parseNotice.value = '尚未读到提取记录，未发起模型调用；请重试读取后再提取。'
        return false
      }
    }
    if (source !== `${resumeId.value}:${version.value}` || parseBusy.value) return false
    if (parsePending.value && !submissionMissing.value) {
      return readParseDraft(pendingRequest.value || parseDraft.value!.id)
    }
    if (!pendingRequest.value && parseDraft.value && !window.confirm('重新提取会发起一次新的模型调用。已有候选和项目会保留，继续吗？')) return false
    stopParseWatch()
    const token = generation
    const id = resumeId.value
    const sourceVersion = version.value
    const requestId = pendingRequest.value || crypto.randomUUID()
    pendingRequest.value = requestId
    parseBusy.value = true; parseError.value = ''; parseNotice.value = ''; parseElapsed.value = 0; parseStage.value = '正在提交提取请求…'
    try {
      const draft = await apiRequest<ParseDraft>(`/materials/resumes/${id}/parse-drafts`, postJson({ request_id: requestId, source_version: sourceVersion }), 20_000)
      if (token === generation) {
        await observe(draft, token, id)
        return token === generation && ['ready', 'confirmed'].includes(parseDraft.value?.status ?? '')
      }
    } catch (cause) {
      if (token !== generation) return false
      if (cause instanceof RequestError && ((cause.status >= 400 && cause.status < 500) || cause.code === 'model_not_configured')) pendingRequest.value = ''
      parseError.value = cause instanceof Error ? cause.message : '项目提取失败。'
      parseNotice.value = pendingRequest.value ? '提交结果尚未确认。请读取原任务，不要重复提取。' : ''
    } finally { if (token === generation) stopParseWatch() }
    return false
  }
  async function runLocalExtraction() {
    if (!resumeId.value || parseLoading.value || backupBusy.value) return
    // 只停止页面对 AI 的等待，不取消后台，也不重发 AI 调用。
    stopParseWatch()
    const token = generation
    const id = resumeId.value
    const sourceVersion = version.value
    if (!backupRequest) backupRequest = parseHistory.value.find((item) => item.provider === 'local_rules')?.id || crypto.randomUUID()
    parseBusy.value = true; backupBusy.value = true; parseError.value = ''; parseNotice.value = ''; parseElapsed.value = 0; parseStage.value = '正在按原文生成备选候选…'
    try {
      const draft = await apiRequest<ParseDraft>(`/materials/resumes/${id}/parse-drafts`, postJson({
        request_id: backupRequest, source_version: sourceVersion, mode: 'rule',
      }), 20_000)
      if (token !== generation) return
      applyParseDraft(draft)
      parseNotice.value = '本地备选未调用模型。核对项目边界与事实后再保存；AI 记录可从提取记录中读取。'
    } catch (cause) {
      if (token === generation) parseError.value = cause instanceof Error ? cause.message : '生成本地备选失败，仍可手工添加项目。'
    } finally { if (token === generation) stopParseWatch() }
  }
  watch([resumeId, version], () => {
    stopParseWatch()
    parseDraft.value = null; parseHistory.value = []; pendingRequest.value = ''
    submissionMissing.value = false
    backupRequest = ''
    historyRequest = null; historyLoaded = false
    parseLoading.value = false; parseError.value = ''; parseNotice.value = ''; parseElapsed.value = 0
    if (resumeId.value) void loadParseHistory()
  }, { immediate: true, flush: 'sync' })
  onBeforeUnmount(stopParseWatch)
  return {
    parseDraft, parseHistory, parseBusy, backupBusy, parseLoading, parseError, parseNotice, parseElapsed, parseStage, parsePending, submissionMissing,
    runExtraction, runLocalExtraction, readParseDraft, loadParseHistory, stopWaiting, applyParseDraft,
  }
}
