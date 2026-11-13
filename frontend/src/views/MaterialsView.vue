<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { apiRequest, postJson, putJson, withIdempotencyKey } from '../api/common'
import { useResumeExtraction } from '../composables/useResumeExtraction'

interface Resume { id: string; name: string; source_text: string; source_file_name: string; document_id: string | null; is_active: boolean; content_version: number; revision: number; created_at: string; updated_at: string }
interface Fact { id?: string; fact_type: string; content: string; evidence_quote: string; source_kind?: string; needs_confirmation?: string[] }
interface Project { id: string; resume_id: string; source_version: number; name: string; summary: string; role: string; duration: string; tech_stack: string[]; user_verified: boolean; facts: Fact[]; revision: number }
interface Version { version: number; name: string; source_file_name: string; document_id: string | null; created_at: string }

const resumes = ref<Resume[]>([])
const resume = ref<Resume | null>(null)
const projects = ref<Project[]>([])
const versions = ref<Version[]>([])
const versionDetail = ref<{ version: number; source_text: string } | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const editingResume = ref(false)
const resumeDraft = ref({ name: '', source_text: '', source_file_name: '', document_id: '' as string | null, is_active: true })
const showResumeForm = ref(false)
const ocrMode = ref('auto')
const pdfInput = ref<HTMLInputElement | null>(null)
async function selectPdf(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  await uploadPdf(file)
}
interface DocumentPreview { id: string; file_name: string; extracted_text: string; page_count: number; warnings: string[]; ocr_pages: number[]; cache_hit?: boolean }
const documentPreview = ref<DocumentPreview | null>(null)
const project = ref<Project | null>(null)
const projectFormOpen = ref(false)
const projectDraft = ref({ name: '', summary: '', role: '', duration: '', tech_stack: '', user_verified: false, facts: [] as Fact[] })
const projectBusy = ref(false)
const projectBaseline = ref('')
const projectDirty = computed(() => projectFormOpen.value && JSON.stringify(projectDraft.value) !== projectBaseline.value)
const remainingCandidates = computed(() => parseDraft.value?.result?.projects
  .map((_, index) => index).filter((index) => !parseDraft.value?.confirmed_projects[String(index)]) ?? [])
const candidateIndex = ref<number | null>(null)
const candidateContext = ref<{ id: string; revision: number; source_version: number } | null>(null)
const formSourceVersion = ref(1)
const resumeBlocks = ref<string[]>([])
const resumeBlocksTruncated = ref(false)
const selectedBlocks = ref<number[]>([])
const resumeBlocksBusy = ref(false)
const resumeCreateKey = ref(crypto.randomUUID())
const projectCreateKey = ref(crypto.randomUUID())
const blockCache = new Map<string, { blocks: string[]; truncated: boolean }>()
let blockRequest = 0
const legacyOrigin = import.meta.env.DEV ? 'http://127.0.0.1:8000' : ''
const selectedVersion = computed(() => resume.value?.content_version ?? 1)
const currentSourceLabel = computed(() => resume.value ? `简历 v${selectedVersion.value}` : '未选择简历')
const resumeId = computed(() => resume.value?.id ?? '')
const {
  parseDraft, parseHistory, parseBusy, backupBusy, parseLoading, parseError, parseNotice, parseElapsed, parseStage, parsePending, submissionMissing,
  runExtraction, runLocalExtraction, readParseDraft, loadParseHistory, stopWaiting, applyParseDraft,
} = useResumeExtraction(resumeId, selectedVersion)
const projectReviewWarnings = computed(() => candidateIndex.value === null ? [] : [
  ...(parseDraft.value?.result?.needs_confirmation ?? []),
  ...(parseDraft.value?.result?.projects[candidateIndex.value]?.needs_confirmation ?? []),
])
const materialTab = ref<'projects' | 'source'>('projects')
let resumeRequest = 0
let projectRequest = 0
const verifiedProjects = computed(() => projects.value.filter((item) => item.user_verified && item.source_version === selectedVersion.value && item.facts.length))
const resumeStage = computed(() => !resume.value || showResumeForm.value ? 1
  : parseBusy.value ? 2
  : projectFormOpen.value || remainingCandidates.value.length ? 3
  : verifiedProjects.value.length ? 4 : 2)
const resumeSteps = ['上传并核对文字', 'AI 提取项目', '核对并保存项目', '创建面试计划']
watch(() => JSON.stringify({ ...projectDraft.value, user_verified: undefined }), () => {
  if (projectFormOpen.value) projectDraft.value.user_verified = false
}, { flush: 'sync' })
const PROJECT_HINTS = ['项目', '负责', '开发', '实现', '设计', '搭建', '优化', '主导', '参与', '维护', '系统', '平台']
const hasProjectBlock = computed(() => resumeBlocks.value.some((block) => PROJECT_HINTS.some((word) => block.includes(word))))

onBeforeRouteLeave(() => {
  if (editingResume.value && !window.confirm('简历草稿尚未保存，离开将放弃草稿。继续吗？')) return false
  if (projectDirty.value && !window.confirm('项目草稿尚未保存，离开将放弃草稿。继续吗？')) return false
  if (parseBusy.value && !window.confirm('AI 提取仍在服务端后台进行；离开后结果会保存在草稿中，返回后点击「读取本次结果」即可，不会重复调用模型。继续吗？')) return false
})

async function loadResumes() {
  const result = await apiRequest<{ items: Resume[] }>('/materials/resumes?limit=50&offset=0')
  resumes.value = result.items
  if (!resume.value && result.items.length) await selectResume(result.items[0].id)
}
async function selectResume(id: string) {
  if (projectBusy.value || (busy.value && editingResume.value)) return
  if (editingResume.value && !window.confirm('简历草稿尚未保存，切换将放弃草稿。继续吗？')) return
  if (projectDirty.value && !window.confirm('项目草稿尚未保存，切换将放弃草稿。继续吗？')) return
  const token = ++resumeRequest
  ++projectRequest
  loading.value = true; error.value = ''; project.value = null; versionDetail.value = null
  documentPreview.value = null; projectFormOpen.value = false
  try {
    const next = await apiRequest<Resume>(`/materials/resumes/${id}`)
    if (token !== resumeRequest) return
    resume.value = next
    resumeDraft.value = { name: resume.value.name, source_text: resume.value.source_text, source_file_name: resume.value.source_file_name, document_id: resume.value.document_id, is_active: resume.value.is_active }
    editingResume.value = false; showResumeForm.value = false; candidateIndex.value = null
    const [projectPage, versionPage] = await Promise.all([
      apiRequest<{ items: Project[] }>(`/materials/resumes/${id}/projects?limit=50&offset=0`),
      apiRequest<{ items: Version[] }>(`/materials/resumes/${id}/versions?limit=20&offset=0`),
    ])
    if (token !== resumeRequest) return
    projects.value = projectPage.items; versions.value = versionPage.items
  } catch (cause) { if (token === resumeRequest) error.value = cause instanceof Error ? cause.message : '读取简历资料失败。' }
  finally { if (token === resumeRequest) loading.value = false }
}
function newResume() {
  if (busy.value || projectBusy.value) return
  if (editingResume.value && !window.confirm('简历草稿尚未保存，放弃后新建？')) return
  if (projectDirty.value && !window.confirm('项目草稿尚未保存，放弃后新建简历？')) return
  ++resumeRequest
  resume.value = null; projects.value = []; versions.value = []; showResumeForm.value = true; editingResume.value = true
  resumeCreateKey.value = crypto.randomUUID(); documentPreview.value = null; projectFormOpen.value = false
  resumeDraft.value = { name: '', source_text: '', source_file_name: '', document_id: null, is_active: true }
}
function cancelResumeEdit() {
  if (resume.value) {
    resumeDraft.value = { name: resume.value.name, source_text: resume.value.source_text, source_file_name: resume.value.source_file_name, document_id: resume.value.document_id, is_active: resume.value.is_active }
  } else {
    resumeDraft.value = { name: '', source_text: '', source_file_name: '', document_id: null, is_active: true }
  }
  showResumeForm.value = false; editingResume.value = false; documentPreview.value = null
}
function editResumeSource() {
  if (!resume.value || busy.value || projectBusy.value) return
  if (projectDirty.value && !window.confirm('项目草稿尚未保存，修正简历将放弃当前项目草稿。继续吗？')) return
  projectFormOpen.value = false; candidateIndex.value = null; ++projectRequest
  resumeDraft.value = { name: resume.value.name, source_text: resume.value.source_text,
    source_file_name: resume.value.source_file_name, document_id: resume.value.document_id, is_active: resume.value.is_active }
  showResumeForm.value = true; editingResume.value = true
  notice.value = '请对照原 PDF 手工修正缺失文字。保存将形成新版本；已有项目与计划仍保留旧版本依据。'
}
async function saveResume(andExtract = false) {
  if (busy.value) return
  if (!resumeDraft.value.name.trim() || !resumeDraft.value.source_text.trim()) { error.value = '简历名称和文字内容不能为空。'; return }
  busy.value = true; error.value = ''
  const payload = { ...resumeDraft.value, document_id: resumeDraft.value.document_id || null }
  let saved = false
  try {
    resume.value = resume.value
      ? await apiRequest<Resume>(`/materials/resumes/${resume.value.id}`, putJson({ ...payload, expected_revision: resume.value.revision }))
      : await apiRequest<Resume>('/materials/resumes', withIdempotencyKey(postJson(payload), resumeCreateKey.value))
    showResumeForm.value = false; editingResume.value = false; notice.value = '简历版本已保存。'
    await loadResumes(); if (resume.value) await selectResume(resume.value.id)
    saved = true
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存简历失败。' }
  finally { busy.value = false }
  if (saved && andExtract) {
    notice.value = '简历已保存，正在读取任务记录并准备 AI 填写项目…'
    await fillProjectsWithAI()
  }
}
async function uploadPdf(file?: File) {
  if (!file || busy.value || projectBusy.value) return
  if (editingResume.value && !window.confirm('简历草稿尚未保存，上传 PDF 将放弃草稿。继续吗？')) return
  if (projectDirty.value && !window.confirm('项目草稿尚未保存，上传 PDF 将放弃草稿。继续吗？')) return
  busy.value = true; error.value = ''; notice.value = '正在读取 PDF 并提取文字…'
  try {
    const params = new URLSearchParams({ file_name: file.name, ocr_mode: ocrMode.value })
    documentPreview.value = await apiRequest<DocumentPreview>(`/materials/documents/pdf?${params}`, { method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: file, signal: AbortSignal.timeout(600_000) }, 600_000)
    resumeCreateKey.value = crypto.randomUUID()
    resumeDraft.value = { name: file.name.replace(/\.pdf$/i, ''), source_text: documentPreview.value.extracted_text, source_file_name: file.name, document_id: documentPreview.value.id, is_active: true }
    showResumeForm.value = true; editingResume.value = true; resume.value = null; project.value = null; versionDetail.value = null; projects.value = []; versions.value = []
    notice.value = documentPreview.value.cache_hit
      ? `该 PDF 与退出前保存的原件字节相同，已直接复用「${ocrMode.value === 'force' ? '整份 OCR' : ocrMode.value === 'off' ? '仅提取已有文字' : '仅文字不足页 OCR'}」的已有识别结果。需要真正重跑识别，保存后点「重新提取」。`
      : `PDF 提取完成：${documentPreview.value.page_count} 页。核对文字后再保存简历。`
  } catch (cause) { error.value = cause instanceof Error ? cause.message : 'PDF 识别失败。' }
  finally { busy.value = false }
}
async function reextract() {
  if (!resume.value?.document_id || busy.value) return
  if (editingResume.value && !window.confirm('重新识别将替换当前文字草稿，继续吗？')) return
  busy.value = true; error.value = ''
  notice.value = '正在重新识别原件，会真正重跑一次识别（整份 OCR 可能需要数分钟）…'
  const startedAt = Date.now()
  try {
    documentPreview.value = await apiRequest<DocumentPreview>(`/materials/documents/${resume.value.document_id}/extract?ocr_mode=${ocrMode.value}`, { method: 'POST' }, 600_000)
    resumeDraft.value = { name: resume.value.name, source_text: documentPreview.value.extracted_text, source_file_name: resume.value.source_file_name, document_id: resume.value.document_id, is_active: resume.value.is_active }
    showResumeForm.value = true; editingResume.value = true
    const seconds = ((Date.now() - startedAt) / 1000).toFixed(1)
    const ocrPages = documentPreview.value.ocr_pages.length ? `OCR 页面 ${documentPreview.value.ocr_pages.join('、')}` : '未使用 OCR'
    notice.value = `重新识别完成（${seconds} 秒，${ocrPages}）。这是草稿；保存后才会形成新简历版本。`
  } catch (cause) { notice.value = ''; error.value = cause instanceof Error ? cause.message : '重新识别失败。' }
  finally { busy.value = false }
}
async function formatText() {
  busy.value = true
  try {
    const result = await apiRequest<{ source_text: string }>('/materials/text/format', postJson({ source_text: resumeDraft.value.source_text }))
    resumeDraft.value.source_text = result.source_text; notice.value = '排版草稿已更新；内容未保存，请核对后再保存。'
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '整理文字失败。' }
  finally { busy.value = false }
}
async function loadVersion(version: number) {
  if (!resume.value) return
  const id = resume.value.id
  try {
    const detail = await apiRequest<{ version: number; source_text: string }>(`/materials/resumes/${id}/versions/${version}`)
    if (resume.value?.id === id) versionDetail.value = detail
  }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取版本失败。' }
}
async function openProject(item: Project) {
  if (projectBusy.value) return
  if (projectDirty.value && !window.confirm('项目有未保存的修改，放弃并切换吗？')) return
  const resumeId = resume.value?.id
  if (!resumeId) return
  const token = ++projectRequest
  candidateIndex.value = null; projectFormOpen.value = false
  try {
    const next = await apiRequest<Project>(`/materials/resumes/${resumeId}/projects/${item.id}`)
    if (token !== projectRequest || resume.value?.id !== resumeId) return
    project.value = next
    projectDraft.value = { name: project.value.name, summary: project.value.summary, role: project.value.role, duration: project.value.duration, tech_stack: project.value.tech_stack.join(', '), user_verified: project.value.user_verified, facts: project.value.facts.map((fact) => ({ ...fact })) }
    projectBaseline.value = JSON.stringify(projectDraft.value)
    projectFormOpen.value = true
    void loadResumeBlocks(project.value.source_version)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取项目失败。' }
}
function newProject() {
  if (projectBusy.value) return
  if (projectDirty.value && !window.confirm('项目草稿尚未保存，放弃并新建？')) return
  ++projectRequest
  project.value = null; candidateIndex.value = null; projectFormOpen.value = true
  projectCreateKey.value = crypto.randomUUID()
  projectDraft.value = { name: '', summary: '', role: '', duration: '', tech_stack: '', user_verified: false, facts: [{ fact_type: '职责', content: '', evidence_quote: '' }] }
  projectBaseline.value = JSON.stringify(projectDraft.value)
  void loadResumeBlocks(selectedVersion.value)
}
function addFact(target: Fact[]) { target.push({ fact_type: '项目描述', content: '', evidence_quote: '' }) }
const MAX_BLOCK_CHARS = 800
const MAX_BLOCKS = 300
function splitResumeBlocks(text: string): { blocks: string[]; truncated: boolean } {
  const paragraphs: string[] = []
  let current: string[] = []
  const flush = () => { const joined = current.join('\n').trim(); if (joined) paragraphs.push(joined); current = [] }
  for (const line of text.split(/\r?\n/)) {
    if (line.trim()) current.push(line)
    else flush()
  }
  flush()
  const blocks: string[] = []
  for (const paragraph of paragraphs) {
    if (paragraph.length <= MAX_BLOCK_CHARS) { blocks.push(paragraph); continue }
    for (const line of paragraph.split('\n')) {
      const trimmed = line.trim()
      if (trimmed) blocks.push(trimmed)
    }
  }
  if (blocks.length === 1 && blocks[0].includes('\n')) {
    const lines = blocks[0].split('\n').map((line) => line.trim()).filter(Boolean)
    return { blocks: lines.slice(0, MAX_BLOCKS), truncated: lines.length > MAX_BLOCKS }
  }
  return { blocks: blocks.slice(0, MAX_BLOCKS), truncated: blocks.length > MAX_BLOCKS }
}
function precheckBlocks() {
  selectedBlocks.value = resumeBlocks.value
    .map((text, index) => ({ text, index }))
    .filter(({ text }) => projectDraft.value.facts.some((fact) => fact.evidence_quote === text))
    .map(({ index }) => index)
}
async function loadResumeBlocks(version: number) {
  const token = ++blockRequest
  formSourceVersion.value = version
  selectedBlocks.value = []
  resumeBlocks.value = []
  resumeBlocksTruncated.value = false
  resumeBlocksBusy.value = false
  if (!resume.value) return
  const cacheKey = `${resume.value.id}:${version}`
  const cached = blockCache.get(cacheKey)
  if (cached) {
    resumeBlocks.value = cached.blocks
    resumeBlocksTruncated.value = cached.truncated
    precheckBlocks()
    return
  }
  resumeBlocksBusy.value = true
  try {
    const detail = await apiRequest<{ version: number; source_text: string }>(`/materials/resumes/${resume.value.id}/versions/${version}`)
    const result = splitResumeBlocks(detail.source_text)
    blockCache.set(cacheKey, result)
    if (token !== blockRequest) return
    resumeBlocks.value = result.blocks
    resumeBlocksTruncated.value = result.truncated
    precheckBlocks()
  } catch (cause) {
    if (token === blockRequest) error.value = cause instanceof Error ? cause.message : '读取简历原文失败。'
  } finally {
    if (token === blockRequest) resumeBlocksBusy.value = false
  }
}
function toggleBlock(index: number) {
  const text = resumeBlocks.value[index]
  if (!text) return
  if (selectedBlocks.value.includes(index)) {
    selectedBlocks.value = selectedBlocks.value.filter((item) => item !== index)
    projectDraft.value.facts = projectDraft.value.facts.filter(
      (fact) => !(fact.evidence_quote === text && fact.content === text),
    )
    return
  }
  selectedBlocks.value = [...selectedBlocks.value, index]
  projectDraft.value.facts = projectDraft.value.facts.filter(
    (fact) => fact.content.trim() || fact.evidence_quote.trim(),
  )
  if (!projectDraft.value.facts.some((fact) => fact.evidence_quote === text)) {
    projectDraft.value.facts.push({ fact_type: '职责', content: text, evidence_quote: text })
  }
}
async function saveProject() {
  if (!resume.value) return
  if (projectBusy.value) return
  if (!projectDraft.value.name.trim()) { error.value = '请填写项目名称。'; return }
  if (candidateIndex.value !== null && !projectDraft.value.user_verified) { error.value = '请核对项目与原文依据，并勾选确认后保存。'; return }
  projectBusy.value = true; error.value = ''
  const confirmingCandidate = candidateIndex.value !== null
  const payload = { name: projectDraft.value.name, summary: projectDraft.value.summary, role: projectDraft.value.role, duration: projectDraft.value.duration, tech_stack: projectDraft.value.tech_stack.split(/[,，]/).map((item) => item.trim()).filter(Boolean), user_verified: projectDraft.value.user_verified, facts: projectDraft.value.facts.filter((item) => item.content.trim()).map(({ fact_type, content, evidence_quote }) => ({ fact_type, content, evidence_quote })) }
  try {
    if (candidateIndex.value !== null && candidateContext.value) {
      const candidate = candidateContext.value
      await apiRequest(`/materials/resumes/${resume.value.id}/parse-drafts/${candidate.id}/confirm`, postJson({ ...payload, candidate_index: candidateIndex.value, expected_revision: candidate.revision }))
      applyParseDraft(await apiRequest<NonNullable<typeof parseDraft.value>>(`/materials/resumes/${resume.value.id}/parse-drafts/${candidate.id}`))
    } else if (project.value) {
      project.value = await apiRequest<Project>(`/materials/resumes/${resume.value.id}/projects/${project.value.id}`, putJson({ ...payload, expected_revision: project.value.revision }))
    } else {
      project.value = await apiRequest<Project>(`/materials/resumes/${resume.value.id}/projects`, withIdempotencyKey(postJson({ ...payload, source_version: formSourceVersion.value }), projectCreateKey.value))
    }
    projectFormOpen.value = false; candidateIndex.value = null
    projects.value = (await apiRequest<{ items: Project[] }>(`/materials/resumes/${resume.value.id}/projects?limit=50&offset=0`)).items
    notice.value = '项目已保存；所依据的简历版本保持固定。'
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存项目失败。' }
  finally { projectBusy.value = false }
  if (!error.value && confirmingCandidate && remainingCandidates.value.length && parseDraft.value?.status === 'ready') {
    editCandidate(remainingCandidates.value[0])
    notice.value = '上一项目已保存，请继续核对下一个项目。'
  }
}
function closeProjectForm() {
  if (projectBusy.value) return
  if (projectDirty.value && !window.confirm('项目修改尚未保存，放弃本次修改并关闭表单？')) return
  projectFormOpen.value = false; candidateIndex.value = null
}
function editCandidate(index: number) {
  const item = parseDraft.value?.result?.projects[index]
  if (!item || projectBusy.value) return
  if (projectDirty.value && !window.confirm('项目草稿尚未保存，放弃并载入候选？')) return
  ++projectRequest
  const draft = parseDraft.value!
  candidateContext.value = { id: draft.id, revision: draft.revision, source_version: draft.source_version }
  candidateIndex.value = index; project.value = null; projectFormOpen.value = true
  projectDraft.value = { name: item.name, summary: item.summary ?? '', role: item.role ?? '', duration: item.duration ?? '', tech_stack: item.tech_stack.join(', '), user_verified: false, facts: item.facts.map((fact) => ({ ...fact })) }
  projectBaseline.value = JSON.stringify(projectDraft.value)
  void loadResumeBlocks(parseDraft.value?.source_version ?? selectedVersion.value)
}
async function fillProjectsWithAI() {
  const source = `${resumeId.value}:${selectedVersion.value}`
  const completed = await runExtraction()
  if (source !== `${resumeId.value}:${selectedVersion.value}` || editingResume.value || projectFormOpen.value) return
  if (parseError.value) {
    notice.value = '简历已保存，但项目填写未完成；可读取原任务或使用本地规则备选。'
    return
  }
  if (completed && remainingCandidates.value.length) {
    editCandidate(remainingCandidates.value[0])
    notice.value = '项目候选已填入表单，请逐个核对后保存。未明确写出的技术栈等字段可以留空。'
  } else if (completed) {
    notice.value = parseDraft.value?.result?.projects.length
      ? '本次提取记录中的项目均已确认，可查看已保存项目；不会重复创建。'
      : '本次未提取到可核对的项目候选。请检查已保存的简历文字，或使用本地规则备选、手工添加项目。'
  } else if (!completed) {
    notice.value = parseNotice.value || '本次未生成新的项目候选；已有简历和项目保留。'
  }
}
async function fillProjectsLocally() {
  const source = `${resumeId.value}:${selectedVersion.value}`
  await runLocalExtraction()
  if (source !== `${resumeId.value}:${selectedVersion.value}` || parseError.value || editingResume.value || projectFormOpen.value) return
  if (remainingCandidates.value.length) editCandidate(remainingCandidates.value[0])
}
async function deleteProject(item: Project) {
  if (!resume.value || !window.confirm(`删除项目“${item.name}”？`)) return
  try { await apiRequest(`/materials/resumes/${resume.value.id}/projects/${item.id}?expected_revision=${item.revision}`, { method: 'DELETE' }); projects.value = projects.value.filter((projectItem) => projectItem.id !== item.id); project.value = null }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '删除项目失败。' }
}
async function deleteResume() {
  if (!resume.value || !window.confirm('停用并移除这份简历？已引用此版本的项目与计划仍会保留。')) return
  try { await apiRequest(`/materials/resumes/${resume.value.id}?expected_revision=${resume.value.revision}`, { method: 'DELETE' }); resume.value = null; await loadResumes() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '移除简历失败。' }
}
onMounted(async () => { loading.value = true; try { await loadResumes() } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取简历列表失败。' } finally { loading.value = false } })
</script>

<template>
  <main class="work-page materials-page">
    <input ref="pdfInput" type="file" accept="application/pdf,.pdf" hidden aria-label="选择简历 PDF" @change="selectPdf" />
    <div class="page-heading"><div><p class="eyebrow">RESUME / PROJECT EXPERIENCE</p><h1>简历</h1><p class="heading-intro">管理简历原件与文字，核对 AI 提取的项目经历。已有简历可直接选择使用。</p></div><div v-if="resume && !showResumeForm" class="heading-actions"><button class="button button-primary" :disabled="busy || loading || projectBusy" @click="pdfInput?.click()">上传新简历</button><details class="resume-other-method"><summary>其他添加方式</summary><button class="text-action" :disabled="busy || loading || projectBusy" @click="newResume()">粘贴文字建立简历</button></details></div></div>
    <div class="materials-flow" aria-label="简历准备流程"><span v-for="(label, index) in resumeSteps" :key="label" :class="{ active: resumeStage === index + 1, done: resumeStage > index + 1 }" :aria-current="resumeStage === index + 1 ? 'step' : undefined">{{ index + 1 }} {{ label }}</span></div><div class="materials-layout">
      <aside class="materials-rail"><p class="eyebrow">SAVED RESUMES</p><button v-for="item in resumes" :key="item.id" :disabled="busy || projectBusy" class="resume-choice" :class="{ active: resume?.id === item.id }" @click="selectResume(item.id)"><span class="resume-symbol">R</span><span><strong>{{ item.name }}</strong><small>{{ item.is_active ? '使用中' : '已停用' }} · v{{ item.content_version }}</small></span></button><p v-if="!resumes.length" class="quiet-copy">保存简历后会显示在这里。</p><div v-if="resume" class="version-rail"><p class="eyebrow">VERSION HISTORY</p><button v-for="version in versions" :key="version.version" :class="{ current: version.version === resume.content_version }" @click="loadVersion(version.version)"><span>v{{ version.version }}</span><small>{{ version.name }}</small><i v-if="version.version === resume.content_version">当前</i></button></div></aside>
      <section class="materials-main">
        <div v-if="showResumeForm" class="resume-editor"><div class="section-title"><div><p class="eyebrow">{{ resume ? 'EDIT DRAFT' : 'NEW RESUME' }}</p><h2>{{ resume ? '核对简历文字' : resumeDraft.document_id ? '核对 PDF 提取文字' : '粘贴并核对简历文字' }}</h2></div><button class="text-action" @click="cancelResumeEdit()">取消</button></div><p class="resume-step-help">先核对识别文字，保存后 AI 才会分析这一版本并填写项目；你也可以仅保存，稍后再提取。</p><label class="editor-label">简历名称</label><input v-model="resumeDraft.name" class="material-input" maxlength="200" /><label class="verified-choice"><input v-model="resumeDraft.is_active" type="checkbox" /> 设为当前使用简历</label><div class="file-source" v-if="resumeDraft.document_id"><span>PDF 原件已关联 · {{ resumeDraft.source_file_name }}</span><a :href="`${legacyOrigin}/materials/documents/${resumeDraft.document_id}/file`" target="_blank">查看原件 ↗</a></div><div v-if="documentPreview" class="extract-summary"><b>{{ documentPreview.page_count }} 页</b><span>OCR 页面：{{ documentPreview.ocr_pages.length ? documentPreview.ocr_pages.join('、') : '无' }}</span><small v-for="warning in documentPreview.warnings" :key="warning">{{ warning }}</small></div><label class="editor-label">提取文字 / 简历正文</label><textarea v-model="resumeDraft.source_text" class="resume-text-editor" rows="18" placeholder="粘贴简历文字。原文会按版本保存。"></textarea><div class="editor-toolbar"><label>重新识别策略 <select v-model="ocrMode"><option value="auto">仅文字不足页 OCR</option><option value="force">整份 OCR</option><option value="off">关闭 OCR</option></select></label><button v-if="resume?.document_id" class="text-action" @click="reextract()">重新提取</button><button class="text-action" @click="formatText()">整理断行草稿</button><button class="button button-outline button-small" :disabled="busy" @click="saveResume()">仅保存简历</button><button class="button button-primary button-small" :disabled="busy" @click="saveResume(true)">{{ busy ? '保存中…' : '保存并 AI 填写项目' }}</button></div></div>
        <template v-else-if="resume">
          <div class="material-overview"><div><p class="eyebrow">SAVED SOURCE · VERSION {{ resume.content_version }}</p><h2>{{ resume.name }}</h2><p>{{ resume.source_file_name || '手工文字资料' }} <span>·</span> 更新于 {{ new Date(resume.updated_at).toLocaleDateString('zh-CN') }}</p></div><div class="overview-actions"><a v-if="resume.document_id" class="text-action" :href="`${legacyOrigin}/materials/documents/${resume.document_id}/file`" target="_blank" rel="noopener">PDF 原件 ↗</a><button class="button button-outline button-small" @click="editResumeSource()">编辑文字</button><button class="text-action danger-action" @click="deleteResume()">移除</button></div></div>
          <div v-if="versionDetail" class="version-preview"><div><strong>简历 v{{ versionDetail.version }} 只读预览</strong><button class="text-action" @click="versionDetail = null">关闭</button></div><pre>{{ versionDetail.source_text }}</pre></div>
          <div class="material-tabs" role="group" aria-label="当前简历内容"><button :class="{ active: materialTab === 'projects' }" @click="materialTab = 'projects'">简历中的项目 <small>{{ projects.length }}</small></button><button :class="{ active: materialTab === 'source' }" @click="materialTab = 'source'">简历原文 · v{{ selectedVersion }}</button></div>
          <section v-if="materialTab === 'source'" class="saved-source"><div class="section-title"><h3>已保存原文</h3><a v-if="resume.document_id" :href="`${legacyOrigin}/materials/documents/${resume.document_id}/file`" target="_blank" rel="noopener">打开 PDF 原件 ↗</a></div><p>此文字是项目引用的版本依据；编辑并保存后形成新版本。</p><pre>{{ resume.source_text }}</pre></section>
          <section v-show="materialTab === 'projects'" class="project-workspace"><div class="section-title"><div><p class="eyebrow">PROJECTS / EVIDENCE</p><h2>项目经历 <small>依据 {{ currentSourceLabel }}</small></h2></div><div class="project-actions"><button class="button button-primary button-small" :disabled="parseBusy || parseLoading || loading || projectFormOpen" @click="fillProjectsWithAI()">{{ parseBusy ? '提取中…' : parseLoading ? '读取记录…' : submissionMissing ? '重试提交原请求' : parsePending ? '读取本次结果' : parseDraft ? '重新 AI 填写' : 'AI 填写项目' }}</button><button class="button button-outline button-small" :disabled="backupBusy || parseLoading || loading || projectFormOpen" @click="fillProjectsLocally()">{{ backupBusy ? '生成备选…' : '本地规则备选' }}</button><button class="button button-outline button-small" :disabled="projectBusy || loading" @click="newProject()">手工添加</button></div></div>
            <div class="extraction-intro"><p>AI 分析当前已保存版本的简历文字，将项目名称、技术栈、简介和多条描述填入表单。核对后保存为这份简历的项目经历；不会重新识别 PDF 或修改简历原文。</p><button class="text-action" :disabled="parseBusy || parseLoading || projectFormOpen" @click="loadParseHistory()">读取提取记录</button></div>
            <div v-if="parseHistory.length" class="extraction-history"><label for="parse-record">提取记录</label><select id="parse-record" :value="parseDraft?.id" :disabled="parseBusy || parseLoading || projectFormOpen" @change="readParseDraft(($event.target as HTMLSelectElement).value)"><option v-for="item in parseHistory" :key="item.id" :value="item.id">{{ new Date(item.created_at).toLocaleString('zh-CN') }} · {{ item.provider === 'local_rules' ? '本地规则' : 'AI' }} · {{ item.status === 'running' ? '处理中' : item.status === 'failed' ? '失败' : item.status === 'confirmed' ? '已全部保存' : '待核对' }}</option></select><button v-if="parseDraft?.status === 'running' && !parseBusy" class="text-action" @click="readParseDraft(parseDraft.id)">继续等待原任务</button></div>
            <div v-if="parseError" class="extraction-error" role="alert"><strong>提取未完成</strong><p>{{ parseError }}</p><small v-if="parseDraft?.error_code && parseError === parseDraft.error_message">错误标识：{{ parseDraft.error_code }}</small><p v-if="parseDraft?.elapsed_ms != null && parseError === parseDraft.error_message">耗时 {{ (parseDraft.elapsed_ms / 1000).toFixed(1) }} 秒 · {{ parseDraft.model }}</p><p>可以改用本地规则备选，不依赖网络或模型；结果仍需核对后保存。</p><button class="button button-outline button-small" :disabled="backupBusy || parseLoading || projectFormOpen" @click="fillProjectsLocally()">按原文生成备选候选</button></div>
            <p v-if="parseNotice" class="extraction-note" role="status">{{ parseNotice }}</p>
            <p v-if="parseDraft?.status === 'running' && !parseBusy" class="extraction-note">发现未结束的原任务。读取结果只查询状态，不会重新调用模型；服务重启中断的任务会在到期后标记失败。</p>
            <div v-if="parseBusy" class="parse-progress" role="status" aria-live="polite"><div class="parse-progress-head"><span class="spinner"></span><b>{{ parseStage }}</b><span class="parse-clock">已用 {{ parseElapsed }} 秒</span></div><p>{{ backupBusy ? '本地规则正在读取已保存原文，不调用模型。' : 'AI 在后台处理，页面每 2 秒读取一次草稿。可停止等待或直接改用本地规则；后台 AI 任务不受影响。' }}结果均需核对后保存。</p><div class="parse-progress-actions"><span>模型处理时长不固定，没有可用进度百分比。</span><button class="text-action" type="button" @click="stopWaiting()">停止等待（后台继续）</button></div></div>
            <div v-if="parseDraft?.result && !projectFormOpen" class="candidate-review"><div class="candidate-review-title"><span><b>{{ parseDraft.provider === 'local_rules' ? '本地规则候选 · 未调用模型' : 'AI 候选 · 待人工核对' }}</b><small>读取已保存简历 v{{ parseDraft.source_version }}；{{ parseDraft.provider === 'local_rules' ? '按原文分组，需核对项目边界与事实归属。' : '模型置信度不代表真实性。' }}</small></span><span class="status-chip">{{ parseDraft.result.projects.length }} 个候选</span></div><ul v-if="parseDraft.result.needs_confirmation.length" class="extraction-warnings"><li v-for="warning in parseDraft.result.needs_confirmation" :key="warning">{{ warning }}</li></ul><p v-if="!parseDraft.result.projects.length" class="extraction-note">{{ parseDraft.provider === 'local_rules' ? '未找到可靠的项目边界。请手工添加项目并选取原文事实，规则不会把整份简历当成一个项目。' : '没有通过原文范围校验的候选。请查看提示，或使用本地规则备选。' }}</p><article v-for="(candidate, index) in parseDraft.result.projects" :key="index" class="ai-candidate"><div><strong>{{ candidate.name }}</strong><span>{{ candidate.facts.length }} 条事实 · 待核对原文依据</span><p v-if="candidate.summary">{{ candidate.summary }}</p><details><summary>查看原文依据与待核对项</summary><blockquote>{{ candidate.evidence_quote }}</blockquote></details><div v-if="candidate.needs_confirmation.length" class="candidate-quality" role="note"><strong>保存前需核对</strong><ul><li v-for="warning in candidate.needs_confirmation" :key="warning">{{ warning }}</li></ul><button class="text-action" @click="editResumeSource()">对照原件修正简历文字 →</button><small>可先在候选编辑中移除残缺事实，保留其他完整内容。请核对引用范围；确有原文错误时修正为新版，旧候选不会自动换绑。</small></div></div><button v-if="parseDraft.confirmed_projects[String(index)]" class="status-chip chip-green">已确认</button><button v-else class="text-action" @click="editCandidate(index)">填写此项目 →</button></article></div>
            <section v-if="projectFormOpen" class="project-editor">
              <div class="editor-title"><div><strong>{{ candidateIndex !== null ? (parseDraft?.provider === 'local_rules' ? '核对本地备选项目' : '核对 AI 填写的项目') : project ? '编辑项目' : '填写项目' }}</strong><p>名称、技术栈、简介和多条描述组成一份项目资料；来源依据按需展开。</p></div><button class="text-action" :disabled="projectBusy" @click="closeProjectForm()">关闭表单</button></div>
              <div v-if="candidateIndex !== null && parseDraft?.result" class="project-draft-tabs" role="group" aria-label="提取的项目"><button v-for="(item, index) in parseDraft.result.projects" :key="index" :disabled="projectBusy || !!parseDraft.confirmed_projects[String(index)]" :aria-pressed="candidateIndex === index" :class="{ active: candidateIndex === index }" @click="editCandidate(index)">{{ item.name }}{{ parseDraft.confirmed_projects[String(index)] ? ' · 已保存' : '' }}</button></div>
<div v-if="projectReviewWarnings.length" class="candidate-quality" role="note"><strong>以下内容需要核对</strong><ul><li v-for="warning in projectReviewWarnings" :key="warning">{{ warning }}</li></ul></div>
              <fieldset class="project-form-fields" :disabled="projectBusy">
                <div class="project-fields"><label>项目名称<input v-model="projectDraft.name" maxlength="200" placeholder="项目名称" /></label><label>技术栈<input v-model="projectDraft.tech_stack" placeholder="用逗号分隔，如 Python, FastAPI, LangGraph" /></label><label class="full-field">项目简介<textarea v-model="projectDraft.summary" rows="4" placeholder="项目面向谁、解决什么问题、整体实现了什么。"></textarea></label></div>
                <div class="description-heading"><div><h3>项目描述</h3><p>每条记录一项职责、实现、决策或成果；AI 会从简历中逐条填写。</p></div><button class="text-action" type="button" @click="addFact(projectDraft.facts)">＋ 添加一条</button></div>
                <article v-for="(fact, index) in projectDraft.facts" :key="index" class="description-item">
                  <div class="description-item-head"><label :for="`project-description-${index}`">描述 {{ index + 1 }}</label><button class="text-action danger-action" type="button" @click="projectDraft.facts.splice(index, 1)">移除</button></div>
                  <textarea :id="`project-description-${index}`" v-model="fact.content" rows="4" maxlength="10000" placeholder="填写这一条的具体项目经历，不需要重复填写来源原文。"></textarea>
                  <ul v-if="fact.needs_confirmation?.length" class="fact-quality"><li v-for="warning in fact.needs_confirmation" :key="warning">{{ warning }}</li></ul>
                  <details class="description-evidence"><summary>{{ fact.evidence_quote ? `查看原文依据 · 简历 v${formSourceVersion}` : '手工补充 · 类型和来源' }}</summary><label>描述类型<input v-model="fact.fact_type" maxlength="50" /></label><label>原文引用<textarea v-model="fact.evidence_quote" rows="3" placeholder="无简历引用可留空，保存为手工补充。"></textarea></label></details>
                </article>
                <p v-if="!projectDraft.facts.length" class="picker-hint">尚无项目描述。可以添加一条，或关闭表单后让 AI 从简历填写。</p>
                <details class="project-extra-fields"><summary>其他信息（可选）</summary><div class="project-fields"><label>角色<input v-model="projectDraft.role" /></label><label>周期<input v-model="projectDraft.duration" /></label></div></details>
                <details class="resume-picker"><summary>从简历 v{{ formSourceVersion }} 原文补充描述（辅助） <small>逐字回填，不调用模型</small></summary><p class="picker-hint">勾选段落会同时填入事实内容与原文引用；可继续编辑类型和措辞。未找到项目段落时请手动添加。</p><p v-if="!resumeBlocksBusy && resumeBlocks.length && !hasProjectBlock" class="picker-hint">未在简历原文中发现明显的项目段落，可手动填写事实。</p><div v-if="resumeBlocksBusy" class="picker-hint">正在读取简历原文…</div><div v-else-if="resumeBlocks.length" class="picker-list"><label v-for="(block, index) in resumeBlocks" :key="index" class="picker-block"><input type="checkbox" :checked="selectedBlocks.includes(index)" @change="toggleBlock(index)" /><span>{{ block }}</span></label></div><p v-else class="picker-hint">未读取到可回填的简历原文，请手动填写事实。</p><p v-if="resumeBlocksTruncated" class="picker-hint">原文较长，仅显示前 {{ resumeBlocks.length }} 段；其余内容可手动引用。</p></details>
                <label class="verified-choice"><input v-model="projectDraft.user_verified" type="checkbox" /> 我已核对项目内容</label>
              </fieldset>
              <div class="editor-toolbar"><span>保存后固定引用简历 v{{ formSourceVersion }}；原文不会被改写。</span><button class="button button-primary button-small" :disabled="projectBusy || !projectDraft.name.trim() || (candidateIndex !== null && !projectDraft.user_verified)" @click="saveProject()">{{ projectBusy ? '保存中…' : candidateIndex !== null && remainingCandidates.length > 1 ? '保存并核对下一个' : '保存项目' }}</button></div>
            </section>
            <div v-if="verifiedProjects.length && !projectFormOpen" class="materials-next"><span>当前版本已有 {{ verifiedProjects.length }} 个已核对项目</span><RouterLink :to="{ path: '/planner', query: { resume: resume.id, version: selectedVersion } }">用这些资料创建计划 →</RouterLink></div><h3 class="saved-projects-heading">这份简历的项目 <small>按来源版本保存</small></h3><div class="project-list"><article v-for="item in projects" :key="item.id" class="project-card" tabindex="0" role="button" @keydown.enter.self="openProject(item)" @keydown.space.self.prevent="openProject(item)" :class="{ selected: project?.id === item.id }" @click="openProject(item)"><div class="project-card-head"><div><span class="project-state" :class="item.user_verified ? 'verified' : ''">{{ item.user_verified ? '已核对' : '待核对' }}</span><h3>{{ item.name }}</h3></div><span class="source-version">依据 v{{ item.source_version }}</span></div><p>{{ item.summary || '暂无项目摘要。' }}</p><div class="tech-tags"><span v-for="tech in item.tech_stack" :key="tech">{{ tech }}</span></div><div class="project-facts"><div v-for="fact in item.facts.slice(0, 3)" :key="fact.id"><b>{{ fact.fact_type }}</b><span>{{ fact.content }}</span><small>{{ fact.evidence_quote ? '简历原文引用' : '手工补充' }}</small></div><button class="text-action danger-action" @click.stop="deleteProject(item)">删除项目</button></div></article><div v-if="parseBusy && !projects.length" class="no-projects"><span>正在等待提取候选…</span><p>后台任务完成后，候选会显示在上方；仍需人工核对才能保存为项目。</p></div><div v-else-if="!projects.length && !parseDraft?.result?.projects.length" class="no-projects"><span>尚未保存项目</span><p>添加已核对项目，或从当前保存版本提取候选后人工核对。</p><button class="text-action" :disabled="projectBusy || loading" @click="newProject()">添加第一个项目 →</button></div></div>
          </section>
        </template>
        <div v-else class="materials-empty"><p class="eyebrow">YOUR RESUME</p><h2>从一份简历开始。</h2><p>上传 PDF，先提取并核对文字，再让 AI 将其中的项目经历填入表单。已有简历可从左侧选择，不必重复上传。</p><button class="button button-primary" :disabled="busy || loading || projectBusy" @click="pdfInput?.click()">上传简历 PDF</button><details class="resume-other-method"><summary>没有 PDF？</summary><button class="text-action" :disabled="busy || loading || projectBusy" @click="newResume()">粘贴文字建立简历</button></details></div>
      </section>
    </div>
    <div v-if="busy || loading" class="busy-banner"><span class="spinner"></span>{{ notice || '正在处理资料…' }}</div>
    <div v-if="error || notice" class="toast" :class="{ 'toast-error': error }" role="status"><span>{{ error || notice }}</span><button @click="error = ''; notice = ''">×</button></div>
  </main>
</template>
