<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import MarkdownText from '../components/MarkdownText.vue'
import { onBeforeRouteLeave } from 'vue-router'
import { apiRequest, patchJson, postJson, withIdempotencyKey } from '../api/common'

interface Question { id: string; question_text: string; standard_answer_text: string; user_notes: string; source: string; category_id: string | null; is_active: boolean; content_version: number; revision: number; created_at: string; updated_at: string }
interface Category { id: string; name: string; revision: number; question_count: number }
interface Preview { id: string; file_name: string; status: 'draft' | 'confirmed'; candidates: Array<{ index: number; question_text: string; standard_answer_text: string; raw_text: string; start_line: number; end_line: number; warnings: string[]; recommended: boolean }>; warnings: string[]; selected_indices: number[] }
interface SourceRead { import_id: string; file_name: string; content_sha256: string; parser_version: string; candidate: { raw_text: string; start_line: number; end_line: number; heading_path: string[] } }

const questions = ref<Question[]>([])
const categories = ref<Category[]>([])
const uncategorizedCount = ref(0)
const selected = ref<Question | null>(null)
const draft = ref({ question_text: '', standard_answer_text: '', user_notes: '', is_active: true, category_id: '' })
const isNew = ref(false)
const editing = ref(false)
const dirty = ref(false)
const busy = ref(false)
const search = ref('')
const activeFilter = ref('')
const categoryFilter = ref('')
const offset = ref(0)
const total = ref(0)
const pageSize = 30
const message = ref('')
const error = ref('')
const queryVersion = ref(0)
const history = ref<Array<{ version: number; question_text: string; standard_answer_text: string; user_notes: string; created_at: string }>>([])
const importPreview = ref<Preview | null>(null)
const importSource = ref<{ file_name: string; content: string } | null>(null)
const importStrategy = ref('auto')
const importBusy = ref(false)
const newCategory = ref('')
const categoryCreateRequest = ref<{ name: string; id: string } | null>(null)
const categoryDialog = ref(false)
const selectedIndices = ref<number[]>([])
const checkedIds = ref<string[]>([])
const bulkMode = ref(false)
const bulkBusy = ref(false)
const selectedQuestions = computed(() => questions.value.filter((item) => checkedIds.value.includes(item.id)))
const allPageSelected = computed(() => questions.value.length > 0 && selectedQuestions.value.length === questions.value.length)
function toggleBulkMode() {
  if (bulkBusy.value) return
  bulkMode.value = !bulkMode.value
  checkedIds.value = []
}
function togglePageSelection() {
  if (!bulkBusy.value) checkedIds.value = allPageSelected.value ? [] : questions.value.map((item) => item.id)
}
const questionCreateKey = ref(crypto.randomUUID())
const moveCategory = ref('')
const detailTab = ref<'answer' | 'notes' | 'history' | 'source'>('answer')
const sourceRead = ref<SourceRead | null>(null)

const categoryName = (id: string | null) => categories.value.find((item) => item.id === id)?.name ?? '未分类'
const dirtyNow = computed(() => isNew.value || (editing.value && dirty.value))
const pages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function markDirty() { dirty.value = true }
function confirmLeave() { return !dirtyNow.value || window.confirm('题目有未保存的修改，确定放弃吗？') }
onBeforeRouteLeave(() => {
  if (bulkBusy.value) return false
  if (!confirmLeave()) return false
  return !importPreview.value || window.confirm('Markdown 导入预览和当前选择尚未确认，离开后需要重新生成预览。继续吗？')
})
function setQuestion(question: Question | null, create = false) {
  if (!confirmLeave()) return
  if (create) questionCreateKey.value = crypto.randomUUID()
  selected.value = question
  isNew.value = create
  editing.value = create
  dirty.value = false
  detailTab.value = 'answer'
  history.value = []
  draft.value = question ? {
    question_text: question.question_text, standard_answer_text: question.standard_answer_text,
    user_notes: question.user_notes, is_active: question.is_active, category_id: question.category_id ?? '',
  } : { question_text: '', standard_answer_text: '', user_notes: '', is_active: true, category_id: '' }
}

async function loadCategories() {
  const result = await apiRequest<{ items: Category[]; uncategorized_count: number }>('/question-bank/categories')
  categories.value = result.items
  uncategorizedCount.value = result.uncategorized_count
}
async function loadQuestions() {
  const params = new URLSearchParams({ limit: String(pageSize), offset: String(offset.value) })
  if (search.value.trim()) params.set('search', search.value.trim())
  if (activeFilter.value) params.set('is_active', activeFilter.value)
  if (categoryFilter.value === 'none') params.set('uncategorized', 'true')
  else if (categoryFilter.value) params.set('category_id', categoryFilter.value)
  const result = await apiRequest<{ items: Question[]; total: number }>(`/question-bank/questions?${params}`)
  questions.value = result.items
  total.value = result.total
  if (selected.value) {
    const current = result.items.find((item) => item.id === selected.value?.id)
    if (current && !dirtyNow.value) setQuestion(current)
  }
  if (!selected.value && !isNew.value && result.items.length) setQuestion(result.items[0])
}
async function reload() {
  error.value = ''
  try { await Promise.all([loadCategories(), loadQuestions()]) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取题库失败。' }
}
watch([search, activeFilter, categoryFilter], () => { offset.value = 0; checkedIds.value = []; queryVersion.value++ })
watch(queryVersion, () => { void loadQuestions().catch((cause: unknown) => { error.value = cause instanceof Error ? cause.message : '读取题目失败。' }) })

async function save() {
  if (!draft.value.question_text.trim()) { error.value = '题干不能为空。'; return }
  busy.value = true; error.value = ''
  try {
    if (isNew.value) {
      const result = await apiRequest<Question>('/question-bank/questions', withIdempotencyKey(postJson({
        question_text: draft.value.question_text, standard_answer_text: draft.value.standard_answer_text,
        user_notes: draft.value.user_notes, is_active: draft.value.is_active,
        category_id: draft.value.category_id || null,
      }), questionCreateKey.value), 30_000)
      message.value = '题目已保存，首个内容版本已建立。'
      isNew.value = false; selected.value = result
    } else if (selected.value) {
      const baseline = selected.value
      const payload: Record<string, unknown> = { expected_revision: baseline.revision }
      for (const key of ['question_text', 'standard_answer_text', 'user_notes', 'is_active'] as const) {
        if (draft.value[key] !== baseline[key]) payload[key] = draft.value[key]
      }
      const category = draft.value.category_id || null
      if (category !== baseline.category_id) payload.category_id = category
      if (Object.keys(payload).length === 1) { editing.value = false; dirty.value = false; return }
      selected.value = await apiRequest<Question>(`/question-bank/questions/${baseline.id}`, patchJson(payload))
      message.value = '更改已保存。原文内容变化时会记录新版本。'
    }
    editing.value = false; dirty.value = false
    await reload()
    if (selected.value) setQuestion(selected.value)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存失败。' }
  finally { busy.value = false }
}
function cancelEdit() { if (!confirmLeave()) return; if (isNew.value) setQuestion(null); else if (selected.value) setQuestion(selected.value) }
async function removeQuestion() {
  if (!selected.value || !window.confirm('删除题目？历史版本和来源引用会保留。')) return
  try {
    await apiRequest(`/question-bank/questions/${selected.value.id}?expected_revision=${selected.value.revision}`, { method: 'DELETE' })
    message.value = '题目已从当前题库移除。'; selected.value = null; await reload()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '删除失败。' }
}
async function loadHistory() {
  if (!selected.value) return
  detailTab.value = 'history'
  try { history.value = (await apiRequest<{ items: typeof history.value }>(`/question-bank/questions/${selected.value.id}/versions`)).items }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取历史版本失败。' }
}
async function loadSource() {
  if (!selected.value) return
  try { sourceRead.value = await apiRequest<SourceRead>(`/question-bank/questions/${selected.value.id}/source`); detailTab.value = 'source' }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取来源失败。' }
}
async function createCategory() {
  const name = newCategory.value.trim()
  if (!name) return
  try {
    let requestId = categoryCreateRequest.value?.name === name ? categoryCreateRequest.value.id : ''
    if (!requestId) {
      requestId = crypto.randomUUID()
      categoryCreateRequest.value = { name, id: requestId }
    }
    await apiRequest('/question-bank/categories', postJson({ name, request_id: requestId }))
    categoryCreateRequest.value = null; newCategory.value = ''; categoryDialog.value = false; await loadCategories(); message.value = '分类已创建。'
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '创建分类失败。' }
}
async function renameCategory(category: Category) {
  const name = window.prompt('分类名称', category.name)?.trim()
  if (!name || name === category.name) return
  try { await apiRequest(`/question-bank/categories/${category.id}`, patchJson({ name, expected_revision: category.revision })); await loadCategories(); message.value = '分类已重命名。' }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '重命名分类失败。' }
}
async function deleteCategory(category: Category) {
  if (!window.confirm(`删除分类“${category.name}”？只允许删除空分类。`)) return
  try { await apiRequest('/question-bank/categories/batch-delete', postJson({ categories: [{ id: category.id, expected_revision: category.revision }] })); if (categoryFilter.value === category.id) categoryFilter.value = ''; await loadCategories() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '删除分类失败。分类需为空。' }
}
const emptyCategories = computed(() => categories.value.filter((item) => item.question_count === 0))
const categoryCleanBusy = ref(false)
async function cleanEmptyCategories() {
  const empty = emptyCategories.value
  if (!empty.length || categoryCleanBusy.value) return
  if (!window.confirm(`删除 ${empty.length} 个空分类？题目、来源与已确认计划不受影响。`)) return
  categoryCleanBusy.value = true
  try {
    const result = await apiRequest<{ deleted: number }>('/question-bank/categories/batch-delete', postJson({
      categories: empty.map((item) => ({ id: item.id, expected_revision: item.revision })),
    }))
    if (empty.some((item) => item.id === categoryFilter.value)) categoryFilter.value = ''
    await loadCategories()
    await reload()
    error.value = ''
    message.value = `已删除 ${result.deleted} 个空分类。`
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '清理空分类失败，请刷新后重试。'
  } finally {
    categoryCleanBusy.value = false
  }
}
async function bulkMove() {
  if (bulkBusy.value || busy.value) return
  const moves = selectedQuestions.value.map((item) => ({ id: item.id, expected_revision: item.revision }))
  if (!moves.length) return
  bulkBusy.value = true; error.value = ''; message.value = ''
  try { await apiRequest('/question-bank/questions/classify', postJson({ category_id: moveCategory.value || null, questions: moves })); checkedIds.value = []; await reload(); message.value = '所选题目已移动分类，原文内容保持不变。' }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '移动分类失败。请刷新后重试。' }
  finally { bulkBusy.value = false }
}
async function bulkDelete() {
  if (bulkBusy.value || busy.value) return
  const moves = selectedQuestions.value.map((item) => ({ id: item.id, expected_revision: item.revision }))
  const deletesCurrent = moves.some((item) => item.id === selected.value?.id)
  if (deletesCurrent && !confirmLeave()) return
  if (!moves.length || !window.confirm(`删除当前页选中的 ${moves.length} 道题？历史记录和来源引用会保留。`)) return
  bulkBusy.value = true; error.value = ''; message.value = ''
  try {
    const result = await apiRequest<{ deleted: number }>('/question-bank/questions/batch-delete', postJson({ questions: moves }))
    checkedIds.value = []
    if (deletesCurrent) { selected.value = null; editing.value = false; dirty.value = false; history.value = []; sourceRead.value = null }
    const remaining = Math.max(0, total.value - result.deleted)
    offset.value = Math.min(offset.value, Math.max(0, Math.ceil(remaining / pageSize) - 1) * pageSize)
    await reload()
    message.value = `已删除 ${result.deleted} 道题，历史版本和来源引用保留。`
  }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '批量删除失败。请刷新后重试。' }
  finally { bulkBusy.value = false }
}
async function acceptFile(file?: File) {
  if (!file) return
  if (!/\.(md|markdown)$/i.test(file.name)) { error.value = '请选择 .md 或 .markdown 文件。'; return }
  if (file.size > 1024 * 1024) { error.value = 'Markdown 文件最多 1 MiB。'; return }
  importPreview.value = null; error.value = ''
  try {
    const content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer())
    importSource.value = { file_name: file.name, content }
    await createImportPreview()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '生成导入预览失败。' }
}
async function createImportPreview() {
  if (!importSource.value) return
  importBusy.value = true; error.value = ''
  try {
    const strategy = importStrategy.value === 'h2' ? 'heading_2' : importStrategy.value === 'h3' ? 'heading_3' : importStrategy.value
    const preview = await apiRequest<Preview>('/question-bank/imports', postJson({ ...importSource.value, strategy }), 60_000)
    importPreview.value = preview
    selectedIndices.value = preview.selected_indices
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '生成导入预览失败。' }
  finally { importBusy.value = false }
}
async function confirmImport() {
  if (!importPreview.value || !selectedIndices.value.length) return
  importBusy.value = true; error.value = ''
  try {
    await apiRequest(`/question-bank/imports/${importPreview.value.id}/confirm`, postJson({ selected_indices: selectedIndices.value }), 60_000)
    message.value = `已导入 ${selectedIndices.value.length} 道题。`
    importPreview.value = null; selected.value = null; offset.value = 0; await reload()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '导入确认失败；可保留相同选择后重试。' }
  finally { importBusy.value = false }
}
function selectAll(recommended = false) {
  selectedIndices.value = importPreview.value?.candidates.filter((item) => !recommended || item.recommended).map((item) => item.index) ?? []
}
onMounted(() => { void reload() })
</script>

<template>
  <main class="work-page bank-page">
    <div class="page-heading"><div><p class="eyebrow">YOUR KNOWLEDGE, IN YOUR WORDS</p><h1>个人题库</h1><p class="heading-intro">收集问题，保留自己的答案。需要时，随时回到原文和来源。</p></div><div class="heading-actions" :inert="bulkBusy || undefined"><button class="button button-outline" type="button" :disabled="bulkBusy || busy" :aria-pressed="bulkMode" @click="toggleBulkMode()">{{ bulkMode ? '完成批量管理' : '批量管理' }}</button><label class="button button-outline file-button">导入 Markdown<input type="file" accept=".md,.markdown,text/markdown" @change="acceptFile(($event.target as HTMLInputElement).files?.[0])" /></label><button class="button button-primary" type="button" @click="setQuestion(null, true)">＋ 新建题目</button></div></div>
    <p v-if="bulkBusy" class="bulk-progress" role="status">正在处理所选题目，请稍候…</p><div class="bank-layout">
      <aside class="bank-index" :inert="bulkBusy || undefined">
        <div class="search-wrap"><span>⌕</span><input v-model="search" type="search" placeholder="搜索题干" aria-label="搜索题干" /></div>
        <div class="filter-row"><select v-model="activeFilter" aria-label="启用状态"><option value="">全部状态</option><option value="true">用于面试</option><option value="false">已停用</option></select><button class="small-add" type="button" title="新建分类" @click="categoryDialog = true">＋ 分类</button></div>
        <div class="category-rail"><button class="category-choice" :class="{ active: !categoryFilter }" @click="categoryFilter = ''"><span>全部题目</span><b>{{ total }}</b></button><button v-for="category in categories" :key="category.id" class="category-choice" :class="{ active: categoryFilter === category.id }" @click="categoryFilter = category.id" @contextmenu.prevent="renameCategory(category)" @dblclick.prevent="renameCategory(category)"><span>{{ category.name }}</span><b>{{ category.question_count }}</b></button><button class="category-choice uncategorized" :class="{ active: categoryFilter === 'none' }" @click="categoryFilter = 'none'"><span>未分类</span><b>{{ uncategorizedCount }}</b></button></div>
        <fieldset v-if="bulkMode" class="bulk-tools" :disabled="bulkBusy || busy" :aria-busy="bulkBusy">
          <legend>管理当前页题目</legend>
          <label class="page-selection"><input type="checkbox" :checked="allPageSelected" :indeterminate="selectedQuestions.length > 0 && !allPageSelected" :disabled="!questions.length" @change="togglePageSelection()" />本页全选</label><span role="status">已选 {{ selectedQuestions.length }} / {{ questions.length }} 道</span>
          <label class="bulk-category">移动到<select v-model="moveCategory"><option value="">未分类</option><option v-for="category in categories" :key="category.id" :value="category.id">{{ category.name }}</option></select></label>
          <button :disabled="!selectedQuestions.length" @click="bulkMove()">移动所选</button><button class="danger-action" :disabled="!selectedQuestions.length" @click="bulkDelete()">{{ bulkBusy ? '处理中…' : '删除所选' }}</button><button :disabled="!selectedQuestions.length" @click="checkedIds = []">清空选择</button><small>仅操作当前页；翻页或更换筛选会清空选择。</small>
        </fieldset>
        <div class="list-caption"><span>题目索引</span><span>{{ total }} 条</span></div>
        <div class="question-list">
          <div v-for="(question, index) in questions" :key="question.id" class="question-row-wrap"><input v-if="bulkMode" v-model="checkedIds" :disabled="bulkBusy || busy" type="checkbox" :value="question.id" :aria-label="`选择题目 ${question.question_text}`"/><button class="question-row" :class="{ selected: selected?.id === question.id }" @click="setQuestion(question)"><span class="question-number">{{ String(offset + index + 1).padStart(2, '0') }}</span><span class="question-row-copy"><strong><MarkdownText inline :text="question.question_text" /></strong><small>{{ categoryName(question.category_id) }} <i>·</i> v{{ question.content_version }}</small></span><i class="active-mark" :class="{ off: !question.is_active }"></i></button></div>
          <div v-if="!questions.length && !error" class="list-empty"><span>◌</span><strong>{{ search ? '没有找到匹配题目' : '从一道题开始' }}</strong><small>{{ search ? '试试缩短关键词或清除筛选。' : '导入 Markdown 或手动新建题目。' }}</small></div>
        </div>
        <div class="pagination"><button type="button" :disabled="bulkBusy || offset === 0" @click="checkedIds = []; offset = Math.max(0, offset - pageSize); void loadQuestions()">← 上页</button><span>{{ Math.floor(offset / pageSize) + 1 }} / {{ pages }}</span><button type="button" :disabled="bulkBusy || offset + pageSize >= total" @click="checkedIds = []; offset += pageSize; void loadQuestions()">下页 →</button></div>
      </aside>

      <section class="question-detail" :inert="bulkBusy || undefined">
        <div v-if="selected || isNew" class="detail-header"><div class="detail-meta"><span class="eyebrow">{{ isNew ? 'NEW QUESTION' : `QUESTION ${selected?.content_version?.toString().padStart(2, '0')}` }}</span><span v-if="!isNew" class="status-chip" :class="selected?.is_active ? 'chip-green' : 'chip-muted'">{{ selected?.is_active ? '用于面试' : '已停用' }}</span></div><div class="detail-tools"><button v-if="!isNew && !editing" class="text-action" @click="loadHistory()">版本历史</button><button v-if="!isNew && !editing" class="text-action danger-action" @click="removeQuestion()">删除</button><template v-if="editing"><button class="button button-outline button-small" :disabled="busy" @click="cancelEdit()">取消</button><button class="button button-primary button-small" :disabled="busy" @click="save()">{{ busy ? '保存中…' : '保存更改' }}</button></template><button v-else class="button button-primary button-small" @click="editing = true; dirty = false">编辑题目</button></div></div>
        <div v-if="selected || isNew" class="question-body">
          <template v-if="editing"><label class="editor-label" for="q-title">题干</label><textarea id="q-title" v-model="draft.question_text" class="question-title-input" rows="2" placeholder="写下要练习的问题" @input="markDirty()"></textarea><div class="editor-status"><label><input v-model="draft.is_active" type="checkbox" @change="markDirty()" /> 用于面试抽题</label><select v-model="draft.category_id" @change="markDirty()"><option value="">未分类</option><option v-for="category in categories" :key="category.id" :value="category.id">{{ category.name }}</option></select></div><div class="editor-tabs"><span class="eyebrow">YOUR ANSWER</span></div><textarea v-model="draft.standard_answer_text" class="long-editor" placeholder="写下你认可的回答，支持 Markdown 与代码块。" @input="markDirty()"></textarea><label class="editor-label notes-label" for="q-notes">复习笔记</label><textarea id="q-notes" v-model="draft.user_notes" class="notes-editor" placeholder="个人提示或需要补充的角度" @input="markDirty()"></textarea></template>
          <template v-else><h2 class="question-title"><MarkdownText inline :text="selected?.question_text ?? ''" /></h2><div class="question-meta-line"><span>{{ categoryName(selected?.category_id ?? null) }}</span><span>内容版本 {{ selected?.content_version }}</span><span>更新于 {{ new Date(selected?.updated_at ?? '').toLocaleDateString('zh-CN') }}</span></div><div class="reader-tabs"><button :class="{ active: detailTab === 'answer' }" @click="detailTab = 'answer'">我的答案</button><button :class="{ active: detailTab === 'notes' }" @click="detailTab = 'notes'">复习笔记</button><button :class="{ active: detailTab === 'history' }" @click="loadHistory()">版本记录</button><button v-if="selected?.source !== 'manual'" :class="{ active: detailTab === 'source' }" @click="loadSource()">来源原文</button></div><div v-if="detailTab === 'answer'" class="reader-content"><MarkdownText v-if="selected?.standard_answer_text" :text="selected.standard_answer_text" /><p v-else class="md-empty">还没有填写自己的标准答案。</p></div><div v-else-if="detailTab === 'notes'" class="reader-content notes-content"><MarkdownText v-if="selected?.user_notes" :text="selected.user_notes" /><p v-else class="md-empty">还没有复习笔记。</p></div><div v-else-if="detailTab === 'source' && sourceRead" class="reader-content source-content"><div class="source-heading"><strong>{{ sourceRead.file_name }} · 第 {{ sourceRead.candidate.start_line }}–{{ sourceRead.candidate.end_line }} 行</strong><a :href="`/question-bank/imports/${sourceRead.import_id}/document`">下载完整来源</a></div><pre>{{ sourceRead.candidate.raw_text }}</pre><small>解析器 {{ sourceRead.parser_version }} · SHA-256 {{ sourceRead.content_sha256 }}</small></div><div v-else class="history-content"><article v-for="version in history" :key="version.version" class="version-row"><div><strong>版本 {{ version.version }}</strong><small>{{ new Date(version.created_at).toLocaleString('zh-CN') }}</small></div><p>{{ version.question_text }}</p><details><summary>查看该版原文</summary><pre>{{ version.standard_answer_text }}</pre></details></article><p v-if="!history.length" class="quiet-copy">尚未读取版本记录。</p></div></template>
        </div>
        <div v-else class="detail-empty"><span class="eyebrow">READ / THINK / REFINE</span><h2>一道题，<br />慢慢变成自己的。</h2><p>从左侧选择一道题，完整阅读答案和笔记；需要修改时再进入编辑。</p><button class="text-action" @click="setQuestion(null, true)">新建第一道题 →</button></div>
      </section>
    </div>

    <dialog :open="categoryDialog" class="simple-dialog" @click.self="categoryDialog = false"><form @submit.prevent="createCategory"><p class="eyebrow">ORGANIZE</p><h2>新建分类</h2><input v-model="newCategory" maxlength="60" placeholder="例如：系统设计" /><div class="dialog-actions"><button type="button" class="button button-outline" @click="categoryDialog = false">关闭</button><button class="button button-primary">创建分类</button></div><button v-if="emptyCategories.length" type="button" class="category-clean-action" :disabled="categoryCleanBusy" @click="cleanEmptyCategories()">{{ categoryCleanBusy ? '正在清理…' : `清理空分类（${emptyCategories.length}）` }}</button><div class="category-manage-list"><div v-for="category in categories" :key="category.id"><span>{{ category.name }} · {{ category.question_count }} 题</span><button type="button" @click="renameCategory(category)">重命名</button><button type="button" :disabled="category.question_count > 0" @click="deleteCategory(category)">删除</button></div></div></form></dialog>
    <dialog :open="Boolean(importPreview)" class="import-dialog" @click.self="importPreview = null"><template v-if="importPreview"><header class="import-head"><div><p class="eyebrow">MARKDOWN IMPORT</p><h2>预览并核对候选题</h2><p>{{ importPreview.file_name }} · {{ importPreview.candidates.length }} 个候选</p></div><button class="icon-button" @click="importPreview = null">×</button></header><div class="import-controls"><label>拆分策略 <select v-model="importStrategy"><option value="auto">自动识别</option><option value="h3">三级标题</option><option value="h2">二级标题</option><option value="qa">问答标记</option></select></label><button class="text-action" :disabled="importBusy" @click="createImportPreview()">重新生成预览</button><button class="text-action" @click="selectAll(true)">选中建议项</button><button class="text-action" @click="selectAll(false)">全选</button><button class="text-action" @click="selectedIndices = []">全不选</button></div><p v-for="warning in importPreview.warnings" :key="warning" class="import-warning">{{ warning }}</p><div class="candidate-list"><article v-for="candidate in importPreview.candidates" :key="candidate.index" class="candidate-card"><label class="candidate-check"><input v-model="selectedIndices" type="checkbox" :value="candidate.index" /><span class="candidate-index">{{ String(candidate.index + 1).padStart(2, '0') }}</span></label><div><h3>{{ candidate.question_text || '（题干为空，不能导入）' }}</h3><pre>{{ candidate.standard_answer_text || '没有识别到答案' }}</pre><small>来源行 {{ candidate.start_line }}–{{ candidate.end_line }} <span v-for="warning in candidate.warnings" :key="warning"> · {{ warning }}</span></small></div></article></div><footer class="import-footer"><span>已选择 {{ selectedIndices.length }} 题；确认后会在一个事务中创建题目并保存来源。</span><button class="button button-primary" :disabled="importBusy || !selectedIndices.length" @click="confirmImport()">{{ importBusy ? '正在导入…' : '确认导入' }}</button></footer></template><label v-else class="button button-outline file-button">选择 Markdown<input type="file" accept=".md,.markdown" @change="acceptFile(($event.target as HTMLInputElement).files?.[0])" /></label></dialog>
    <div v-if="message || error" class="toast" :class="{ 'toast-error': error }" role="status"><span>{{ error || message }}</span><button @click="error = ''; message = ''">×</button></div>
  </main>
</template>

