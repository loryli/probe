<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { apiRequest, postJson } from '../api/common'

interface PlanOptions { resumes: Array<{ id: string; name: string; content_version: number; is_active: boolean }>; versions: Array<{ version: number; name: string; content_sha256: string; created_at: string }>; projects: Array<{ id: string; name: string; source_version: number; tech_stack: string[]; fact_count: number }>; questions: Array<{ id: string; question_text: string; content_version: number }>; question_total: number; selected_resume_id: string | null; selected_version: number | null; model: Record<string, unknown>; limits: Record<string, number> }
interface Plan { id: string; resume_id: string | null; source_version: number | null; status: string; revision: number; provider: string; schema_version: string; inputs: { mode: 'model' | 'rule'; resume_id: string | null; source_version: number | null; project_ids: string[]; question_ids: string[]; direction: string; project_ratio: number; duration_minutes: number; max_total_followups: number }; snapshot: { resume_name: string; projects: Array<{ id: string; name: string; facts: Array<{ id: string; fact_type: string; content: string; evidence_quote: string | null }> }>; questions: Array<{ id: string; question_text: string }> } | null; result: { schema_version: string; session_summary: string; objectives: Array<{ id: string; topic: string; source_preference: string; priority: string; observable_evidence: string[]; project_fact_ids: string[]; question_ids: string[]; time_budget_sec: number; max_followups: number; completion_rule: string }>; source_time_budget_sec: Record<string, number>; opening_buffer_sec: number; closing_buffer_sec: number; duration_target_sec: number; constraints: { max_total_followups: number; duration_is_soft: boolean }; limitations: string[]; notes: { system: string[]; model: string[]; policy: string[] }; coverage: { facts_used: number; facts_total: number; questions_used: number; questions_total: number; projects_used: number; projects_total: number } | null } | null; error_code: string | null; error_message: string | null; elapsed_ms: number | null; created_at: string; confirmed_at: string | null }
interface PlanSummary { id: string; status: string; resume_id: string | null; source_version: number | null; direction: string; duration_minutes: number; objective_count: number; created_at: string; confirmed_at: string | null }

const options = ref<PlanOptions | null>(null)
const route = useRoute()
const plans = ref<PlanSummary[]>([])
const plan = ref<Plan | null>(null)
const selectedResume = ref('')
const practiceMode = ref<'question_bank' | 'resume'>('question_bank')
const selectedVersion = ref(1)
const selectedProjects = ref<string[]>([])
const selectedQuestions = ref<string[]>([])
const config = ref({ mode: 'model' as 'model' | 'rule', direction: '', duration_minutes: 45, project_ratio: 0.5, max_total_followups: 6 })
const loading = ref(false)
const generating = ref(false)
const error = ref('')
const notice = ref('')
const step = ref(1)
const planOffset = ref(0)
const planTotal = ref(0)
const dirty = ref(false)
const modelReady = computed(() => Boolean(options.value?.model.configured))
const projectsForVersion = computed(() => options.value?.projects.filter((item) => item.source_version === selectedVersion.value) ?? [])
const maxQuestions = computed(() => options.value?.limits.max_questions ?? 40)
const sourceBalance = computed(() => Math.round(config.value.project_ratio * 100))
const pendingPlanRequest = ref<{ signature: string; id: string } | null>(null)
let syncingOptions = false

onBeforeRouteLeave(() => {
  if (dirty.value && !window.confirm('计划配置尚未生成，离开后会丢弃本页草稿。继续吗？')) return false
})

async function loadHistory() {
  const result = await apiRequest<{ items: PlanSummary[]; total: number }>(`/interview-plans?limit=20&offset=${planOffset.value}`)
  plans.value = result.items; planTotal.value = result.total
}
async function loadOptions(resumeId?: string, version?: number) {
  const params = new URLSearchParams()
  if (resumeId) params.set('resume_id', resumeId)
  if (version) params.set('source_version', String(version))
  options.value = await apiRequest<PlanOptions>(`/interview-plans/options${params.size ? `?${params}` : ''}`)
  if (!selectedResume.value) selectedResume.value = options.value.selected_resume_id ?? options.value.resumes[0]?.id ?? ''
  if (!selectedVersion.value || !resumeId) selectedVersion.value = options.value.selected_version ?? options.value.resumes.find((item) => item.id === selectedResume.value)?.content_version ?? 1
}
async function initialize() {
  loading.value = true; error.value = ''; syncingOptions = true
  try {
    const requestedResume = typeof route.query.resume === 'string' ? route.query.resume : ''
    practiceMode.value = requestedResume ? 'resume' : 'question_bank'
    const requestedVersion = Number(route.query.version)
    await Promise.all([loadOptions(requestedResume || undefined, Number.isInteger(requestedVersion) && requestedVersion > 0 ? requestedVersion : undefined), loadHistory()])
    selectedResume.value = options.value?.selected_resume_id ?? ''
    selectedVersion.value = options.value?.selected_version ?? 1
    selectAllSources(); dirty.value = false
    if (requestedResume) notice.value = `已载入所选简历 v${selectedVersion.value} 的已核对项目。`
  }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '无法读取规划资料。' }
  finally { loading.value = false; syncingOptions = false }
}
function questionOrder(ids: string[]) {
  const order = new Map((options.value?.questions ?? []).map((item, index) => [item.id, index]))
  return [...ids].sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0))
}
function sampleQuestions() {
  const pool = options.value?.questions ?? []
  const indices = pool.map((_, index) => index)
  for (let i = indices.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    ;[indices[i], indices[j]] = [indices[j], indices[i]]
  }
  return indices.slice(0, Math.min(maxQuestions.value, pool.length)).sort((a, b) => a - b).map((index) => pool[index].id)
}
function selectAllSources() { selectedProjects.value = practiceMode.value === 'resume' ? projectsForVersion.value.map((item) => item.id) : []; selectedQuestions.value = sampleQuestions() }
function toggleQuestion(id: string) {
  if (selectedQuestions.value.includes(id)) {
    selectedQuestions.value = selectedQuestions.value.filter((item) => item !== id)
  } else if (selectedQuestions.value.length >= maxQuestions.value) {
    error.value = `最多选择 ${maxQuestions.value} 道题，请先取消其他题目。`
    return
  } else {
    selectedQuestions.value = questionOrder([...selectedQuestions.value, id])
  }
  dirty.value = true
}
watch(selectedResume, async (id) => {
  if (!id || syncingOptions) return
  syncingOptions = true
  loading.value = true
  try { await loadOptions(id); selectedVersion.value = options.value?.selected_version ?? options.value?.resumes.find((item) => item.id === id)?.content_version ?? 1; await loadOptions(id, selectedVersion.value); selectedProjects.value = projectsForVersion.value.map((item) => item.id) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取简历版本失败。' }
  finally { loading.value = false; syncingOptions = false }
})
watch(selectedVersion, async (version, old) => {
  if (!selectedResume.value || version === old || syncingOptions) return
  try { await loadOptions(selectedResume.value, version); selectedProjects.value = projectsForVersion.value.map((item) => item.id) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取版本候选失败。' }
})
function choosePracticeMode(mode: 'question_bank' | 'resume') {
  practiceMode.value = mode
  selectedProjects.value = mode === 'resume' ? projectsForVersion.value.map((item) => item.id) : []
  dirty.value = true
}
function nextStep() {
  if (step.value === 1 && practiceMode.value === 'resume' && !selectedResume.value) { error.value = '请先选择简历，或改为纯题库练习。'; return }
  if (step.value === 2 && !selectedQuestions.value.length && (practiceMode.value === 'question_bank' || !selectedProjects.value.length)) { error.value = '请至少选择一道题或一个已核对项目。'; return }
  step.value = Math.min(3, step.value + 1)
}
function updateConfig<K extends keyof typeof config.value>(key: K, value: (typeof config.value)[K]) { config.value[key] = value; dirty.value = true }
function toggle(id: string, list: string[]) { return list.includes(id) ? list.filter((item) => item !== id) : [...list, id] }
async function generate() {
  if (generating.value || loading.value) return
  if (practiceMode.value === 'resume' && config.value.mode === 'model' && !modelReady.value) { error.value = '请先配置模型，或选择规则规划。'; return }
  if (practiceMode.value === 'resume' && !selectedResume.value) { error.value = '请选择简历，或切换纯题库练习。'; return }
  if ((practiceMode.value === 'question_bank' || !selectedProjects.value.length) && !selectedQuestions.value.length) { error.value = '至少选择一道题目，或选择带描述的已核对项目。'; return }
  generating.value = true; error.value = ''; notice.value = '正在生成训练目标与时间预算…'
  const inputs = {
    mode: practiceMode.value === 'question_bank' ? 'rule' : config.value.mode, resume_id: practiceMode.value === 'resume' ? selectedResume.value : null,
    source_version: practiceMode.value === 'resume' ? selectedVersion.value : null, project_ids: practiceMode.value === 'resume' ? [...selectedProjects.value] : [],
    question_ids: [...selectedQuestions.value], direction: config.value.direction,
    duration_minutes: config.value.duration_minutes, project_ratio: practiceMode.value === 'resume' ? config.value.project_ratio : 0,
    max_total_followups: practiceMode.value === 'resume' ? config.value.max_total_followups : 0,
  }
  const signature = JSON.stringify(inputs)
  let requestId = pendingPlanRequest.value?.signature === signature ? pendingPlanRequest.value.id : ''
  if (!requestId) {
    requestId = crypto.randomUUID()
    pendingPlanRequest.value = { signature, id: requestId }
  }
  try {
    plan.value = await apiRequest<Plan>('/interview-plans', postJson({
      request_id: requestId, ...inputs,
    }), 330_000)
    if (plan.value.status !== 'running') pendingPlanRequest.value = null
    dirty.value = false; notice.value = plan.value.status === 'failed' ? '生成失败，原配置仍保留。' : '规划结果已生成，可检查依据后确认。'
    await loadHistory()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '计划生成失败。输入仍保留；读取本次结果以核对状态。' }
  finally { generating.value = false }
}
async function readPlan(id: string) {
  if (generating.value || loading.value) return
  if (dirty.value && !window.confirm('当前配置尚未生成。离开会保留在本页，仍要读取旧计划吗？')) return
  loading.value = true; error.value = ''; plan.value = null
  try { plan.value = await apiRequest<Plan>(`/interview-plans/${id}`); dirty.value = false }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取计划失败。' }
  finally { loading.value = false }
}
async function confirmPlan() {
  if (loading.value || generating.value) return
  if (!plan.value || plan.value.status !== 'ready' || !window.confirm('确认这份计划？确认后它将作为本场练习的固定依据。')) return
  loading.value = true; error.value = ''
  try { plan.value = await apiRequest<Plan>(`/interview-plans/${plan.value.id}/confirm`, postJson({ expected_revision: plan.value.revision })); notice.value = '计划已确认，可以开始练习。'; await loadHistory() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '计划无法确认；检查资料是否已发生变化。' }
  finally { loading.value = false }
}
async function deletePlan() {
  if (!plan.value || !window.confirm('逻辑删除这条计划？关联历史不会被覆盖。')) return
  try { await apiRequest(`/interview-plans/${plan.value.id}?expected_revision=${plan.value.revision}`, { method: 'DELETE' }); plan.value = null; await loadHistory(); notice.value = '计划已从当前列表移除。' }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '删除计划失败。' }
}
function startNew() { if (dirty.value && !window.confirm('当前配置尚未生成，清空并创建新计划？')) return; plan.value = null; dirty.value = false; step.value = 1; void initialize() }
async function adjustPlan() {
  if (!plan.value) return
  const inputs = plan.value.inputs
  config.value = { mode: inputs.mode, direction: inputs.direction, duration_minutes: inputs.duration_minutes, project_ratio: inputs.project_ratio, max_total_followups: inputs.max_total_followups }
  syncingOptions = true
  try {
    practiceMode.value = inputs.resume_id ? 'resume' : 'question_bank'
    selectedResume.value = inputs.resume_id ?? ''
    selectedVersion.value = inputs.source_version ?? 1
    await loadOptions(inputs.resume_id ?? undefined, inputs.source_version ?? undefined)
    selectedProjects.value = [...inputs.project_ids]
    selectedQuestions.value = [...inputs.question_ids]
    plan.value = null; dirty.value = true; step.value = 1
  } finally { syncingOptions = false }
}
function formatTime(seconds: number) { const m = Math.floor(seconds / 60); const s = seconds % 60; return s ? `${m}分${s}秒` : `${m}分钟` }
onMounted(() => { void initialize() })
</script>

<template>
  <main class="work-page planner-page">
    <div class="page-heading"><div><p class="eyebrow">DIRECTION BEFORE QUESTIONS</p><h1>面试计划</h1><p class="heading-intro">规划考察目标与预算，不生成固定题单。每次调整都会形成新的计划记录。</p></div><button class="button button-primary" @click="startNew()">＋ 新建计划</button></div>
    <div class="planner-layout">
      <aside class="plan-history"><div class="history-title"><p class="eyebrow">SAVED PLANS</p><span>{{ planTotal }}</span></div><button v-for="item in plans" :key="item.id" class="plan-history-item" :class="{ active: plan?.id === item.id }" @click="readPlan(item.id)"><span class="plan-state-dot" :class="`state-${item.status}`"></span><span><strong>{{ item.direction || '未命名方向' }}</strong><small>{{ item.duration_minutes }} 分钟 · {{ new Date(item.created_at).toLocaleDateString('zh-CN') }}</small></span><i>{{ item.status === 'confirmed' ? '已确认' : item.status === 'ready' ? '待确认' : item.status }}</i></button><p v-if="!plans.length" class="quiet-copy">生成后的计划会保留在这里。</p></aside>
      <section class="planner-main">
        <template v-if="plan?.result">
          <div class="plan-result-head"><div><span class="plan-status-badge" :class="`state-${plan.status}`">{{ plan.status === 'confirmed' ? '已确认' : plan.status === 'ready' ? '待确认' : plan.status }}</span><p class="eyebrow">{{ plan.result.schema_version }} · {{ plan.provider }}</p><h2>{{ plan.inputs.direction || '模拟面试计划' }}</h2><p>{{ plan.resume_id ? `${plan.snapshot?.resume_name} · 简历 v${plan.source_version}` : '纯题库练习 · 无需简历和项目' }} · {{ plan.inputs.duration_minutes }} 分钟</p></div><div class="plan-detail-actions"><button class="text-action" @click="adjustPlan()">调整配置并生成新计划 →</button><button class="text-action danger-action" @click="deletePlan()">删除计划</button></div></div>
          <div class="plan-summary"><span class="eyebrow">PLANNING INTENT</span><p>{{ plan.result.session_summary }}</p></div>
          <div class="budget-strip"><div v-if="plan.resume_id"><span>项目经历</span><b>{{ formatTime(plan.result.source_time_budget_sec.project || 0) }}</b></div><div><span>题库问题</span><b>{{ formatTime(plan.result.source_time_budget_sec.question_bank || 0) }}</b></div><div><span>开场 / 收尾</span><b>{{ formatTime(plan.result.opening_buffer_sec) }} / {{ formatTime(plan.result.closing_buffer_sec) }}</b></div><div><span>时间属性</span><b>参考预算 · 不强制到时结束</b></div></div>
          <div class="result-subhead"><div><p class="eyebrow">WHAT THIS SESSION WILL EXPLORE</p><h3>考察目标</h3></div><span>{{ plan.result.objectives.length }} 项</span></div>
          <article v-for="(objective, index) in plan.result.objectives" :key="objective.id" class="objective-card"><div class="objective-number">{{ String(index + 1).padStart(2, '0') }}</div><div class="objective-main"><div class="objective-title-row"><h4>{{ objective.topic }}</h4><span class="priority-tag" :class="`priority-${objective.priority}`">{{ objective.priority === 'high' ? '高优先级' : objective.priority === 'medium' ? '中优先级' : '低优先级' }}</span></div><p>{{ objective.completion_rule || '根据回答中的可观察证据判断目标覆盖。' }}</p><div class="evidence-list"><span v-for="item in objective.observable_evidence" :key="item">{{ item }}</span></div><div class="objective-meta"><span>{{ objective.source_preference === 'project' ? '项目依据' : '题库依据' }}</span><span>{{ formatTime(objective.time_budget_sec) }}</span><span>最多追问 {{ objective.max_followups }} 次</span></div></div></article>
          <section v-if="plan.result.coverage && plan.resume_id" class="coverage-strip"><div><b>{{ plan.result.coverage.facts_used }} / {{ plan.result.coverage.facts_total }}</b><span>项目事实</span></div><div><b>{{ plan.result.coverage.questions_used }} / {{ plan.result.coverage.questions_total }}</b><span>题库问题</span></div><div><b>{{ plan.result.coverage.projects_used }} / {{ plan.result.coverage.projects_total }}</b><span>项目</span></div></section>
          <div v-if="plan.result.limitations.length || plan.result.notes?.policy?.length" class="limitations"><p class="eyebrow">需要留意</p><p v-for="item in [...plan.result.limitations, ...(plan.result.notes?.policy || [])]" :key="item">{{ item }}</p></div>
          <footer class="plan-result-footer"><span>依据快照已固定；再次调整会创建一份新计划。</span><button v-if="plan.status === 'ready'" class="button button-primary" :disabled="loading" @click="confirmPlan()">确认计划</button><RouterLink v-else-if="plan.status === 'confirmed'" class="button button-primary" :to="{ path: '/interviews', query: { plan: plan.id } }">按此计划开始练习 ↗</RouterLink><span v-else>当前状态不可确认</span></footer>
        </template>
        <template v-else-if="plan">
          <section v-if="plan.status === 'running'" class="plan-failure" role="status"><span class="plan-status-badge">处理中</span><h2>计划正在生成。</h2><p>原配置与资料快照已保存。读取同一计划只查询结果，不会重复调用模型。</p><button class="button button-primary" :disabled="loading" @click="readPlan(plan.id)">读取原计划结果</button></section><section v-else class="plan-failure"><span class="plan-status-badge state-failed">生成未完成</span><p class="eyebrow">PLAN REQUEST · {{ plan.provider }}</p><h2>这份计划没有生成结果。</h2><p>{{ plan.error_message || plan.error_code || '原配置仍保留。你可以检查配置后重新发起一次生成。' }}</p><dl><div><dt>训练方向</dt><dd>{{ plan.inputs.direction || '未填写' }}</dd></div><div><dt>资料版本</dt><dd>{{ plan.resume_id ? `${plan.snapshot?.resume_name || '简历'} · v${plan.source_version}` : '纯题库练习' }}</dd></div></dl><button class="button button-primary" @click="adjustPlan()">返回原配置并重新生成 →</button></section>
        </template>
        <template v-else>
          <div v-if="pendingPlanRequest && !generating" class="pending-plan-note" role="status"><p>上一次请求结果尚未确认，先读取原计划可避免重复生成。</p><button class="text-action" :disabled="loading" @click="readPlan(pendingPlanRequest.id)">读取原计划结果 →</button></div>
          <div v-if="practiceMode === 'resume' && options && !options.resumes.length" class="pending-plan-note"><p>先保存一份简历，核对项目后再规划。也可以保存文字简历并选择题库练习。</p><RouterLink to="/materials">准备简历与项目 →</RouterLink></div>
          <fieldset class="plan-config" :disabled="generating || loading">
          <div class="planner-form-head"><div><p class="eyebrow">BUILD A SESSION PLAN</p><h2>先确定训练的边界。</h2><p>选择依据、方向与节奏。完成配置后才会调用规划服务。</p></div><div class="planner-steps"><button v-for="n in 3" :key="n" :class="{ active: step === n, done: step > n }" @click="step = n">0{{ n }}</button></div></div>
          <div v-if="step === 1" class="plan-step">
            <div class="step-intro"><span>01</span><div><h3>选择练习方式</h3><p>没有简历也可以直接练习自己的题库。</p></div></div>
            <div class="practice-mode-choice" role="group" aria-label="练习方式">
              <button type="button" :aria-pressed="practiceMode === 'question_bank'" :class="{ active: practiceMode === 'question_bank' }" @click="choosePracticeMode('question_bank')"><strong>纯题库练习</strong><span>无需上传资料，只问所选题库原题，不额外生成追问。AI 可用于反馈和复盘。</span></button>
              <button type="button" :aria-pressed="practiceMode === 'resume'" :class="{ active: practiceMode === 'resume' }" @click="choosePracticeMode('resume')"><strong>简历项目面试</strong><span>基于已核对项目深入提问，也可以加入题库题。</span></button>
            </div>
            <div v-if="practiceMode === 'resume'" class="form-grid">
              <label>简历<select v-model="selectedResume" @change="dirty = true"><option value="" disabled>选择简历</option><option v-for="item in options?.resumes" :key="item.id" :value="item.id">{{ item.name }}{{ item.is_active ? ' · 使用中' : '' }}</option></select></label>
              <label>简历版本<select v-model.number="selectedVersion" @change="dirty = true"><option v-for="item in options?.versions" :key="item.version" :value="item.version">v{{ item.version }} · {{ item.name }}</option></select></label>
              <label>规划方式<select :value="config.mode" @change="updateConfig('mode', ($event.target as HTMLSelectElement).value as 'model' | 'rule')"><option value="model">AI 规划</option><option value="rule">规则规划</option></select></label>
              <RouterLink to="/materials">上传简历并填写项目 →</RouterLink>
            </div>
            <p v-else class="inline-note">题库练习的计划由程序整理，不调用模型；开始练习时可选择 AI 反馈或规则练习。</p>
          </div>
          <div v-else-if="step === 2" class="plan-step"><div class="step-intro"><span>02</span><div><h3>材料范围</h3><p>候选资料池决定 Planner 可引用什么。题库目标依据会用已启用的题干。</p></div></div><template v-if="practiceMode === 'resume'"><div class="source-heading"><b>项目经历</b><button class="text-action" @click="selectedProjects = selectedProjects.length ? [] : projectsForVersion.map((item) => item.id)">{{ selectedProjects.length ? '清空' : '全选' }}</button></div><label v-for="item in projectsForVersion" :key="item.id" class="source-option"><input type="checkbox" :checked="selectedProjects.includes(item.id)" @change="selectedProjects = toggle(item.id, selectedProjects); dirty = true"/><span><strong>{{ item.name }}</strong><small>{{ item.fact_count }} 条已保存事实 · {{ item.tech_stack.join(' / ') }}</small></span><i>v{{ item.source_version }}</i></label><p v-if="!projectsForVersion.length" class="inline-note">该版本尚无项目事实。可只使用题库题。</p></template><div class="source-heading second-source"><b>个人题库 · {{ options?.question_total || 0 }} 道可用题，已选 {{ selectedQuestions.length }} / {{ maxQuestions }}</b><span class="source-actions"><button class="text-action" @click="selectedQuestions = sampleQuestions(); dirty = true">随机重选 {{ maxQuestions }} 道</button><button class="text-action" @click="selectedQuestions = []; dirty = true">清空</button></span></div><p v-if="options && !options.question_total" class="inline-note">还没有可用题目。<RouterLink to="/">先去个人题库导入、创建或启用题目 →</RouterLink></p><div class="question-source-list"><label v-for="item in options?.questions" :key="item.id" class="question-source"><input type="checkbox" :checked="selectedQuestions.includes(item.id)" @change="toggleQuestion(item.id)"/><span>{{ item.question_text }}</span><small>v{{ item.content_version }}</small></label></div></div>
          <div v-else class="plan-step"><div class="step-intro"><span>03</span><div><h3>方向与节奏</h3><p>规划关注点和时间预算，系统会在目标内部动态提问。</p></div></div><div class="form-grid"><label class="wide-field">训练方向<input :value="config.direction" maxlength="500" placeholder="例如：RAG 可靠性、Agent 状态管理与服务交付" @input="updateConfig('direction', ($event.target as HTMLInputElement).value)"/></label><label>目标时长<select :value="config.duration_minutes" @change="updateConfig('duration_minutes', Number(($event.target as HTMLSelectElement).value))"><option v-for="minutes in [20,30,45,60,90,120]" :key="minutes" :value="minutes">{{ minutes }} 分钟</option></select></label><label v-if="practiceMode === 'resume'">追问总上限<input type="number" min="0" max="30" :value="config.max_total_followups" @input="updateConfig('max_total_followups', Number(($event.target as HTMLInputElement).value))"/></label></div><div v-if="practiceMode === 'resume'" class="ratio-control"><div><b>题源时间比例</b><small>当前项目资料 {{ projectsForVersion.length }} 组 · 题库 {{ options?.questions.length || 0 }} 道</small></div><label>项目 {{ sourceBalance }}%<input type="range" min="0" max="100" :value="sourceBalance" @input="updateConfig('project_ratio', Number(($event.target as HTMLInputElement).value) / 100)"/></label><label>题库 {{ 100 - sourceBalance }}%</label></div><p class="inline-note">目标时长用于节奏参考，不代表强制结束时间。追问边界由总预算和目标约束共同决定。</p></div>
          <footer class="step-footer"><button class="button button-outline" :disabled="step === 1" @click="step--">← 上一步</button><span>第 {{ step }} / 3 步 · 此处仅收集配置</span><button v-if="step < 3" class="button button-primary" @click="nextStep()">继续 →</button><button v-else class="button button-primary" :disabled="generating || loading" @click="generate()">{{ generating ? '正在生成…' : '生成计划' }} ↗</button></footer>
          </fieldset>
        </template>
      </section>
    </div>
    <div v-if="loading || generating" class="busy-banner"><span class="spinner"></span>{{ notice || (generating ? '正在生成计划…' : '正在读取计划…') }}</div><div v-if="error || notice" class="toast" :class="{ 'toast-error': error }" role="status"><span>{{ error || notice }}</span><button @click="error = ''; notice = ''">×</button></div>
  </main>
</template>
