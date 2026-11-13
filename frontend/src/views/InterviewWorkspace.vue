<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useInterview } from '../composables/useInterview'
import type { InterviewTurn } from '../types'

const interview = useInterview()
const historyOpen = ref(true)
const reviewOpen = ref(false)
const recordDialog = ref(false)
const recordDialogEl = ref<HTMLDialogElement | null>(null)
const transcript = ref<HTMLElement | null>(null)
const planChoice = ref<HTMLSelectElement | null>(null)
const now = ref(Date.now())
const observedAt = ref(Date.now())
let timer: number | undefined
watch(interview.session, () => { observedAt.value = Date.now() })
watch(() => interview.session.value?.status, (status) => { reviewOpen.value = status === 'completed' })

// 必须用 showModal 让对话框进入 top layer：仅设 open 属性时，主舞台的大标题
// 会按层叠顺序盖在对话框上，且 ::backdrop 不生效，文字互相压叠。
watch(recordDialog, (open) => {
  const node = recordDialogEl.value
  if (!node) return
  if (open && !node.open) node.showModal()
  else if (!open && node.open) node.close()
})
function onRecordDialogClose() {
  if (interview.deleteBusy.value) return
  recordDialog.value = false
  interview.checkedSessionIds.value = []
}
function onRecordDialogCancel(event: Event) {
  if (interview.deleteBusy.value) event.preventDefault()
}

const selectableSessions = computed(() => interview.sessions.value.filter((item) => item.status !== 'active'))
const allSelectableChecked = computed(() => selectableSessions.value.length > 0
  && selectableSessions.value.every((item) => interview.checkedSessionIds.value.includes(item.id)))
const sessionStatusText = (status: string) => ({ active: '进行中', paused: '已暂停', completed: '已结束' } as Record<string, string>)[status] ?? status

const completedObjectives = computed(() => interview.session.value?.objectives.filter(
  (objective) => objective.status === 'covered' || objective.status === 'skipped',
).length ?? 0)
const objectivePercent = computed(() => {
  const objectives = interview.session.value?.objectives ?? []
  return objectives.length ? Math.round(completedObjectives.value / objectives.length * 100) : 0
})
const remainingLabel = computed(() => {
  const active = interview.session.value
  if (!active) return '00:00'
  const delta = active.status === 'active' ? Math.max(0, Math.floor((now.value - observedAt.value) / 1000)) : 0
  const remaining = active.duration_target_sec - active.elapsed_sec - delta
  const seconds = Math.abs(remaining)
  return `${remaining < 0 ? '已超 ' : ''}${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`
})
const currentObjective = computed(() => interview.session.value?.objectives.find(
  (item) => item.id === interview.currentQuestion.value?.objective_id,
))
function objectiveFor(turn: InterviewTurn) {
  return interview.session.value?.objectives.find((objective) => objective.id === turn.objective_id)
}
function statusText(status: string) {
  return ({ covered: '已覆盖', skipped: '已略过', active: '进行中', pending: '待进入' } as Record<string, string>)[status] ?? status
}
function formatDate(value: string) {
  return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
function scrollTranscript() {
  requestAnimationFrame(() => transcript.value?.scrollTo({ top: transcript.value.scrollHeight, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' }))
}
function scrollToTurn(sequence: number) {
  const node = document.getElementById(`turn-${sequence}`)
  node?.scrollIntoView({ block: 'center', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
  node?.focus({ preventScroll: true })
}
async function submit() {
  await interview.submitAnswer()
  scrollTranscript()
}
function toggleHistory() { historyOpen.value = !historyOpen.value }
function openRecordDialog() {
  interview.checkedSessionIds.value = []
  interview.error.value = ''
  recordDialog.value = true
}
function closeRecordDialog() {
  if (interview.deleteBusy.value) return
  recordDialog.value = false
  interview.checkedSessionIds.value = []
}
function onSessionCheck(id: string, event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  const next = new Set(interview.checkedSessionIds.value)
  if (checked) next.add(id)
  else next.delete(id)
  interview.checkedSessionIds.value = [...next]
}
function onAllSessionCheck(event: Event) {
  interview.checkedSessionIds.value = (event.target as HTMLInputElement).checked
    ? selectableSessions.value.map((item) => item.id)
    : []
}
async function confirmDeleteSessions() {
  if (await interview.deleteSessions()) recordDialog.value = false
}
function keySubmit(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault()
    void submit()
  }
}

onMounted(() => {
  void interview.initialize()
  timer = window.setInterval(() => { now.value = Date.now() }, 1000)
})
onBeforeUnmount(() => { if (timer !== undefined) window.clearInterval(timer) })
</script>

<template>
  <main class="interview-page">

    <div class="workspace-frame">
      <aside class="rail">
        <div class="rail-heading">
          <div>
            <p class="eyebrow">PRACTICE ROOM</p>
            <h1>模拟面试</h1>
          </div>
          <button class="icon-button rail-refresh" type="button" aria-label="刷新本场状态" title="刷新本场状态" @click="interview.refresh()">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16.5 8A6.8 6.8 0 0 0 4.2 6.2L3 7.5m0 0V4.8m0 2.7h2.7M3.5 12a6.8 6.8 0 0 0 12.3 1.8l1.2-1.3m0 0v2.7m0-2.7h-2.7" /></svg>
          </button>
        </div>

        <section v-if="!interview.session.value" class="start-section">
          <div class="start-art" aria-hidden="true"><span class="art-orbit orbit-one"></span><span class="art-orbit orbit-two"></span><span class="art-core">练</span><span class="art-spark">✳</span></div>
          <p class="eyebrow">A SPACE TO THINK</p>
          <h2>把思路<br />说完整。</h2>
          <p class="start-copy">从一个真实目标开始。练习会根据你的回答推进，而不是照着固定题单往下走。</p>

          <div v-if="interview.availablePlans.value.length" class="start-form">
            <label class="field-label" for="plan-choice">本次训练计划</label>
            <select id="plan-choice" ref="planChoice" v-model="interview.selectedPlan.value" class="select-control">
              <option v-for="plan in interview.availablePlans.value" :key="plan.id" :value="plan.id">
                {{ plan.practice_mode === 'question_bank' ? '纯题库' : '项目面试' }} · {{ plan.direction || '未命名方向' }} · {{ plan.duration_minutes }} 分钟
              </option>
            </select>
            <div class="mode-choice" role="group" aria-label="面试模式">
              <button type="button" :class="{ chosen: interview.mode.value === 'model' }" @click="interview.mode.value = 'model'">模型面试</button>
              <button type="button" :class="{ chosen: interview.mode.value === 'rule' }" @click="interview.mode.value = 'rule'">规则练习</button>
            </div>
            <button class="button button-primary button-wide" type="button" :disabled="interview.pageBusy.value" @click="interview.start()">
              <span>{{ interview.pageBusy.value ? '正在准备开场…' : '开始一场练习' }}</span><span aria-hidden="true">↗</span>
            </button>
          </div>
          <div v-else-if="interview.pageBusy.value" class="empty-inline"><span class="spinner"></span>正在读取可用计划</div>
          <div v-else class="empty-inline">先选择练习方式。<RouterLink to="/planner">只练题库（无需简历）或创建项目面试计划 →</RouterLink></div>
        </section>

        <section v-else class="progress-section">
          <div class="session-kicker"><span class="live-pill" :class="`status-${interview.session.value.status}`"><i></i>{{ interview.session.value.status === 'active' ? '进行中' : interview.session.value.status === 'paused' ? '已暂停' : '已结束' }}</span>
            <span class="mode-label">{{ interview.session.value.mode === 'model' ? '模型模式' : '规则模式' }}</span></div>
          <div class="direction-title">{{ interview.session.value.direction }}</div>
          <div class="timer-row"><div><span class="eyebrow">参考时长</span><strong>{{ remainingLabel }}</strong></div><div class="progress-copy">{{ completedObjectives }} / {{ interview.session.value.objectives.length }}<small>目标完成</small></div></div>
          <div class="progress-track" role="progressbar" :aria-valuenow="objectivePercent" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${objectivePercent}%` }"></span></div>

          <div class="objective-head"><span class="eyebrow">本场考察路径</span><span>{{ interview.session.value.objectives.length }} 个目标</span></div>
          <ol class="objective-list">
            <li v-for="(objective, index) in interview.session.value.objectives" :key="objective.id" :class="[`objective-${objective.status}`, { 'objective-current': objective.id === interview.currentQuestion.value?.objective_id }]">
              <span class="objective-index">{{ objective.status === 'covered' ? '✓' : String(index + 1).padStart(2, '0') }}</span>
              <span class="objective-copy"><strong>{{ objective.topic }}</strong><small>{{ objective.source === 'project' ? '项目经历' : '题库问题' }} · {{ statusText(objective.status) }}</small></span>
              <span v-if="objective.id === interview.currentQuestion.value?.objective_id" class="objective-marker" aria-label="当前目标"></span>
            </li>
          </ol>

          <div class="rail-divider"></div>
          <button class="history-toggle" type="button" :aria-expanded="historyOpen" @click="toggleHistory()"><span>最近练习</span><span class="history-count">{{ interview.sessions.value.length }}</span><span class="chevron" :class="{ open: historyOpen }">⌄</span></button>
          <div v-if="historyOpen" class="history-list">
            <RouterLink v-for="item in interview.sessions.value.slice(0, 5)" :key="item.id" :to="`/interviews/${item.id}`" class="history-item" :class="{ selected: item.id === interview.session.value.id }">
              <span class="history-status" :class="`status-${item.status}`"></span><span class="history-item-copy"><strong>{{ item.direction }}</strong><small>{{ formatDate(item.created_at) }} · {{ item.answer_count }} 个回答</small></span>
            </RouterLink>
            <p v-if="!interview.sessions.value.length" class="history-empty">完成一场练习后会出现在这里。</p>
          </div>
        </section>

        <section v-if="!interview.session.value && interview.sessions.value.length" class="recent-starts"><p class="eyebrow">继续练习 / 回看复盘</p><RouterLink v-for="item in interview.sessions.value.slice(0, 5)" :key="item.id" :to="`/interviews/${item.id}`" class="history-item"><span class="history-item-copy"><strong>{{ item.direction || '模拟面试' }}</strong><small>{{ sessionStatusText(item.status) }} · {{ item.answer_count }} 个回答 · {{ formatDate(item.created_at) }}</small></span></RouterLink></section>
        <div class="rail-bottom"><span class="keyboard-hint">⌘ / Ctrl <kbd>↵</kbd> 提交回答</span><div class="rail-bottom-actions"><button class="record-manage-link" type="button" @click="openRecordDialog()">记录管理</button><button class="new-session-link" type="button" @click="interview.newSession()">＋ 新练习</button></div></div>
      </aside>

      <section class="main-stage" :class="{ 'has-session': interview.session.value }">
        <template v-if="interview.session.value">
          <div class="stage-topline">
            <div><p class="eyebrow">{{ currentObjective?.source === 'project' ? 'PROJECT QUESTION' : 'QUESTION BANK' }}<span v-if="interview.currentQuestion.value?.is_followup"> / FOLLOW-UP</span></p><div class="stage-title">{{ currentObjective?.topic || '当前练习' }}</div></div>
            <div class="stage-actions">
              <button v-if="interview.session.value.status === 'active'" class="button button-quiet" type="button" :disabled="interview.isLocked.value" @click="interview.control('pause')">暂停</button>
              <button v-if="interview.session.value.status === 'paused'" class="button button-quiet" type="button" :disabled="interview.isLocked.value" @click="interview.control('resume')">继续</button>
              <button v-if="interview.session.value.status === 'active'" class="button button-quiet" type="button" :disabled="interview.isLocked.value" @click="interview.control('skip')">跳过目标</button>
              <button v-if="interview.session.value.status !== 'completed'" class="button button-end" type="button" :disabled="interview.isLocked.value" @click="interview.control('end')">结束练习</button>
            </div>
          </div>

          <div class="session-status" :class="`phase-${interview.phase.value}`" role="status" aria-live="polite">
            <span class="status-symbol" aria-hidden="true"><i v-if="['streaming','saving','recovering','loading'].includes(interview.phase.value)" class="spinner"></i><i v-else></i></span>
            <span v-if="interview.phase.value === 'saving'">正在提交回答…</span>
            <span v-else-if="interview.phase.value === 'streaming'">{{ interview.session.value.practice_mode === 'question_bank' ? '回答已保存 · 正在反馈，下一题将从题库原题选择' : interview.liveText.value ? '面试官正在回应' : '回答已保存 · 正在生成下一步' }}</span>
            <span v-else-if="interview.phase.value === 'recovering'">正在重新核对本场状态…</span>
            <span v-else-if="interview.session.value.status === 'paused'">本场已暂停，当前记录已保存</span>
            <span v-else-if="interview.session.value.status === 'completed'">本场已结束 · {{ interview.session.value.completion_reason || '问答记录已保存' }}</span>
            <span v-else-if="interview.session.value.error_code">{{ interview.session.value.error_message || '上一步未完成 · 回答仍在记录中' }}</span>
            <span v-else-if="interview.session.value.current_turn?.is_followup">这是针对上一条回答的追问</span>
            <span v-else>{{ interview.notice.value || '问题已就绪，按自己的思路回答即可' }}</span>
            <button v-if="interview.session.value.status === 'active' && (interview.session.value.error_code || (!interview.currentQuestion.value && !interview.isLocked.value))" class="inline-action" type="button" :disabled="interview.isLocked.value" @click="interview.retry()">重试下一步 <span aria-hidden="true">↗</span></button>
            <button v-else-if="interview.phase.value === 'recovering'" class="inline-action" type="button" @click="interview.refresh()">重新读取</button>
          </div>

          <div ref="transcript" class="conversation-scroll">
            <article v-for="turn in interview.session.value.turns" :id="`turn-${turn.sequence}`" :key="turn.id" tabindex="-1" class="turn-row" :class="`turn-${turn.role}`">
              <div v-if="turn.role === 'interviewer'" class="turn-avatar interviewer-avatar">面</div>
              <div class="turn-content">
                <div class="turn-meta"><span>{{ turn.role === 'interviewer' ? '面试官' : '你的回答' }}</span><span>第 {{ turn.sequence }} 轮</span><span v-if="turn.role === 'interviewer' && turn.is_followup" class="followup-tag">追问</span><span v-if="turn.role === 'candidate' && turn.answered" class="saved-tag">已保存</span></div>
                <p class="turn-text">{{ turn.text }}</p>
                <div v-if="turn.role === 'interviewer' && objectiveFor(turn)" class="turn-source">{{ objectiveFor(turn)?.source === 'project' ? '项目事实' : '个人题库' }} <span>·</span> {{ objectiveFor(turn)?.topic }}</div>
                <div v-if="turn.role === 'candidate' && turn.decision?.observation" class="turn-observation">本轮观察：{{ turn.decision.observation }}</div>
              </div>
              <div v-if="turn.role === 'candidate'" class="turn-avatar candidate-avatar">我</div>
            </article>
            <article v-if="interview.liveText.value" class="turn-row turn-interviewer turn-live">
              <div class="turn-avatar interviewer-avatar">面</div><div class="turn-content"><div class="turn-meta"><span>面试官</span><span class="live-label">正在生成</span></div><p class="turn-text">{{ interview.liveText.value }}<span class="typing-caret"></span></p></div>
            </article>
            <div v-else-if="interview.phase.value === 'streaming'" class="thinking-row"><span class="thinking-mark"><i></i><i></i><i></i></span><span>{{ interview.session.value.practice_mode === 'question_bank' ? '正在整理本轮反馈，题库原题会按当前目标继续推进' : '结合你的回答，组织下一步问题' }}</span></div>
            <div v-if="!interview.session.value.turns.length && interview.phase.value === 'loading'" class="thinking-row"><span class="spinner"></span><span>正在准备这场练习…</span></div>
          </div>

          <details v-if="interview.preservedAnswer.value" class="preserved-answer" open><summary>保留的未提交草稿（当前问题已变化）</summary><p>{{ interview.preservedAnswer.value.question }}</p><pre>{{ interview.preservedAnswer.value.text }}</pre><button class="button button-quiet" type="button" :disabled="!interview.canAnswer.value" @click="interview.restoreAnswer()">载入当前回答框</button></details>
          <div v-if="interview.session.value.status === 'paused'" class="paused-prompt"><strong>练习已暂停</strong><p>{{ interview.answer.value.trim() ? '当前回答草稿仍保留在本页；点击继续后可以接着编辑。' : '点击继续返回作答，也可以直接结束并生成复盘。' }}</p></div>
          <form v-if="interview.session.value.status === 'active' && interview.currentQuestion.value" class="answer-composer" @submit.prevent="submit()">
            <div class="composer-label"><label for="answer-draft">你的回答</label><span>{{ interview.answer.value.length.toLocaleString() }} / 8,000</span></div>
            <textarea id="answer-draft" v-model="interview.answer.value" :disabled="!interview.canAnswer.value" maxlength="8000" rows="4" placeholder="从结论开始，再讲清楚你的依据、选择和结果…" @keydown="keySubmit"></textarea>
            <div class="composer-footer"><span>回答会先保存，再开始生成下一步</span><button class="button button-submit" type="submit" :disabled="!interview.canAnswer.value || !interview.answer.value.trim()"><span>{{ interview.phase.value === 'saving' ? '提交中' : '提交回答' }}</span><span aria-hidden="true">↑</span></button></div>
          </form>
          <div v-else-if="interview.session.value.status === 'completed'" class="report-area">
            <div v-if="interview.reportBusy.value" class="report-loading"><span class="spinner"></span>正在整理本场复盘，问答记录已安全保存。</div>
            <template v-else-if="interview.report.value?.report">
              <button class="report-toggle" type="button" :aria-expanded="reviewOpen" @click="reviewOpen = !reviewOpen"><span><b>本场复盘</b><small>{{ interview.report.value.source === 'model' ? '模型分析' : '规则统计' }}</small></span><span>{{ interview.report.value.report.overall === 'solid' ? '进展稳固' : interview.report.value.report.overall === 'weak' ? '需要加强' : '部分达成' }}</span><i>{{ reviewOpen ? '−' : '+' }}</i></button>
              <div v-if="reviewOpen" class="report-content"><p>{{ interview.report.value.report.summary }}</p><div v-for="evaluation in interview.report.value.report.evaluations" :key="evaluation.objective_id" class="evaluation"><div class="evaluation-heading"><strong>{{ interview.session.value.objectives.find((item) => item.id === evaluation.objective_id)?.topic || evaluation.objective_id }}</strong><span>{{ evaluation.status === 'covered' ? '已覆盖' : evaluation.status === 'partial' ? '部分覆盖' : '未覆盖' }}</span></div><p>{{ evaluation.comment }}</p><div v-if="evaluation.turn_sequences.length" class="evidence-links">依据 <button v-for="sequence in evaluation.turn_sequences" :key="sequence" type="button" @click="scrollToTurn(sequence)">回答 {{ sequence }}</button></div></div><div v-if="interview.report.value.report.risks.length" class="suggestions"><strong>复盘限制</strong><ul><li v-for="risk in interview.report.value.report.risks" :key="risk">{{ risk }}</li></ul></div><div v-if="interview.report.value.report.suggestions.length" class="suggestions"><strong>下一步练习</strong><ul><li v-for="item in interview.report.value.report.suggestions" :key="item">{{ item }}</li></ul></div></div>
            </template>
            <div v-else class="report-prompt"><div><strong>{{ interview.report.value?.status === 'failed' ? '复盘生成失败' : '整场复盘' }}</strong><p>{{ interview.report.value?.error_message || '面试已结束。生成复盘查看目标覆盖、回答依据和下一步练习建议。' }}</p></div><button class="button button-primary" type="button" :disabled="interview.reportBusy.value" @click="interview.generateReport()">生成复盘 <span aria-hidden="true">↗</span></button></div>
          </div>
        </template>

        <section v-else class="welcome-panel">
          <div class="welcome-index"><span>01</span><span>练习 · 回答 · 复盘</span><span>2026</span></div>
          <div class="welcome-copy"><p class="eyebrow">YOUR PERSONAL INTERVIEW STUDIO</p><h2>练习，不是背诵。<br /><em>是把思路说清楚。</em></h2><p>用自己的项目和问题做一场有依据的模拟面试。每个回答都会被保存，结束后再回看哪些地方讲清楚了，哪些还值得继续练。</p><button class="welcome-cta" type="button" @click="planChoice?.focus()">从左侧选择计划开始 <span>←</span></button></div>
          <div class="welcome-foot"><span>文字练习现在可用</span><span class="future-channel"><i></i>语音练习 · 后续设计中</span></div>
          <div class="welcome-glyph" aria-hidden="true"><div class="glyph-ring ring-a"></div><div class="glyph-ring ring-b"></div><div class="glyph-cursor"></div><span>思</span><small>STAY WITH THE QUESTION</small></div>
        </section>
      </section>
    </div>

    <div v-if="interview.error.value" class="toast toast-error" role="alert"><span class="toast-mark">!</span><span>{{ interview.error.value }}</span><button type="button" aria-label="关闭提示" @click="interview.error.value = ''">×</button></div>
    <div v-else-if="interview.notice.value" class="toast" role="status"><span class="toast-mark">✓</span><span>{{ interview.notice.value }}</span><button type="button" aria-label="关闭提示" @click="interview.notice.value = ''">×</button></div>
  </main>

  <dialog ref="recordDialogEl" class="record-dialog" aria-labelledby="record-dialog-title" @click.self="closeRecordDialog()" @close="onRecordDialogClose()" @cancel="onRecordDialogCancel($event)">
    <div class="record-dialog-inner">
      <header class="record-dialog-head"><div><p class="eyebrow">PRACTICE RECORDS</p><h2 id="record-dialog-title">批量删除练习记录</h2></div><button class="record-dialog-close" type="button" aria-label="关闭记录管理" :disabled="interview.deleteBusy.value" @click="closeRecordDialog()">×</button></header>
      <p class="record-dialog-note">删除会连同全部问答、目标进度与整场复盘一起物理移除，不可恢复；已确认的面试计划不受影响。进行中的场次需先结束或暂停。这里列出最近 {{ interview.sessions.value.length }} 场记录。</p>
      <div class="record-dialog-tools"><label><input type="checkbox" :checked="allSelectableChecked" :disabled="!selectableSessions.length || interview.deleteBusy.value" @change="onAllSessionCheck($event)" /> 全选可删记录</label><span>已选 {{ interview.checkedSessionIds.value.length }}</span></div>
      <div v-if="interview.sessions.value.length" class="record-dialog-list">
        <label v-for="item in interview.sessions.value" :key="item.id" class="record-dialog-row" :class="{ 'row-locked': item.status === 'active' }">
          <input type="checkbox" :checked="interview.checkedSessionIds.value.includes(item.id)" :disabled="item.status === 'active' || interview.deleteBusy.value" :aria-label="`选择记录：${item.direction || '模拟面试'}`" @change="onSessionCheck(item.id, $event)" />
          <span class="record-dialog-copy"><strong>{{ item.direction || '模拟面试' }}</strong><small>{{ formatDate(item.created_at) }} · {{ item.answer_count }} 个回答 · {{ sessionStatusText(item.status) }}</small></span>
          <em v-if="item.status === 'active'">进行中</em>
        </label>
      </div>
      <p v-else class="record-dialog-empty">还没有面试记录。</p>
      <footer class="record-dialog-foot"><button class="button button-quiet" type="button" :disabled="interview.deleteBusy.value" @click="closeRecordDialog()">取消</button><button class="record-delete-button" type="button" :disabled="!interview.checkedSessionIds.value.length || interview.deleteBusy.value" @click="confirmDeleteSessions()">{{ interview.deleteBusy.value ? '正在删除…' : '删除所选记录' }}</button></footer>
    </div>
  </dialog>
</template>
