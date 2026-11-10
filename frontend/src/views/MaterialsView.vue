<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { apiRequest, postJson, putJson, withIdempotencyKey } from '../api/common'

interface Resume { id: string; name: string; source_text: string; source_file_name: string; document_id: string | null; is_active: boolean; content_version: number; revision: number; created_at: string; updated_at: string }
interface Fact { id?: string; fact_type: string; content: string; evidence_quote: string; source_kind?: string }
interface Project { id: string; resume_id: string; source_version: number; name: string; summary: string; role: string; duration: string; tech_stack: string[]; user_verified: boolean; facts: Fact[]; revision: number }
interface Version { version: number; name: string; source_file_name: string; document_id: string | null; created_at: string }
interface ParseDraft { id: string; revision: number; source_version: number; status: string; result: { projects: Array<{ name: string; summary?: string | null; role?: string | null; duration?: string | null; tech_stack: string[]; evidence_quote: string; facts: Fact[]; confidence: number; needs_confirmation: string[] }> } | null; confirmed_projects: Record<string, string>; error_message: string | null }

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
const documentPreview = ref<{ id: string; file_name: string; extracted_text: string; page_count: number; warnings: string[]; ocr_pages: number[] } | null>(null)
const project = ref<Project | null>(null)
const projectFormOpen = ref(false)
const projectDraft = ref({ name: '', summary: '', role: '', duration: '', tech_stack: '', user_verified: false, facts: [] as Fact[] })
const projectBusy = ref(false)
const parseDraft = ref<ParseDraft | null>(null)
const parseBusy = ref(false)
const candidateIndex = ref<number | null>(null)
const resumeCreateKey = ref(crypto.randomUUID())
const projectCreateKey = ref(crypto.randomUUID())
const parseRequest = ref<{ signature: string; id: string } | null>(null)
const legacyOrigin = import.meta.env.DEV ? 'http://127.0.0.1:8000' : ''
const selectedVersion = computed(() => resume.value?.content_version ?? 1)
const currentSourceLabel = computed(() => resume.value ? `简历 v${selectedVersion.value}` : '未选择简历')

onBeforeRouteLeave(() => {
  if (editingResume.value && !window.confirm('简历草稿尚未保存，离开将放弃草稿。继续吗？')) return false
  if (projectFormOpen.value && !window.confirm('项目草稿尚未保存，离开将放弃草稿。继续吗？')) return false
})

async function loadResumes() {
  const result = await apiRequest<{ items: Resume[] }>('/materials/resumes?limit=50&offset=0')
  resumes.value = result.items
  if (!resume.value && result.items.length) await selectResume(result.items[0].id)
}
async function selectResume(id: string) {
  if (editingResume.value && !window.confirm('简历草稿尚未保存，切换将放弃草稿。继续吗？')) return
  if (projectFormOpen.value && !window.confirm('项目草稿尚未保存，切换将放弃草稿。继续吗？')) return
  loading.value = true; error.value = ''; project.value = null; parseDraft.value = null
  parseRequest.value = null; documentPreview.value = null; projectFormOpen.value = false
  try {
    resume.value = await apiRequest<Resume>(`/materials/resumes/${id}`)
    resumeDraft.value = { name: resume.value.name, source_text: resume.value.source_text, source_file_name: resume.value.source_file_name, document_id: resume.value.document_id, is_active: resume.value.is_active }
    editingResume.value = false
    const [projectPage, versionPage] = await Promise.all([
      apiRequest<{ items: Project[] }>(`/materials/resumes/${id}/projects?limit=50&offset=0`),
      apiRequest<{ items: Version[] }>(`/materials/resumes/${id}/versions?limit=20&offset=0`),
    ])
    projects.value = projectPage.items; versions.value = versionPage.items
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取简历资料失败。' }
  finally { loading.value = false }
}
function newResume() {
  if (editingResume.value && !window.confirm('简历草稿尚未保存，放弃后新建？')) return
  if (projectFormOpen.value && !window.confirm('项目草稿尚未保存，放弃后新建简历？')) return
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
async function saveResume() {
  if (!resumeDraft.value.name.trim() || !resumeDraft.value.source_text.trim()) { error.value = '简历名称和文字内容不能为空。'; return }
  busy.value = true; error.value = ''
  const payload = { ...resumeDraft.value, document_id: resumeDraft.value.document_id || null }
  try {
    resume.value = resume.value
      ? await apiRequest<Resume>(`/materials/resumes/${resume.value.id}`, putJson({ ...payload, expected_revision: resume.value.revision }))
      : await apiRequest<Resume>('/materials/resumes', withIdempotencyKey(postJson(payload), resumeCreateKey.value))
    showResumeForm.value = false; editingResume.value = false; notice.value = '简历版本已保存。'
    await loadResumes(); if (resume.value) await selectResume(resume.value.id)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存简历失败。' }
  finally { busy.value = false }
}
async function uploadPdf(file?: File) {
  if (!file) return
  if (editingResume.value && !window.confirm('简历草稿尚未保存，上传 PDF 将放弃草稿。继续吗？')) return
  if (projectFormOpen.value && !window.confirm('项目草稿尚未保存，上传 PDF 将放弃草稿。继续吗？')) return
  busy.value = true; error.value = ''; notice.value = '正在读取 PDF 并提取文字…'
  try {
    const params = new URLSearchParams({ file_name: file.name, ocr_mode: ocrMode.value })
    documentPreview.value = await apiRequest<{ id: string; file_name: string; extracted_text: string; page_count: number; warnings: string[]; ocr_pages: number[] }>(`/materials/documents/pdf?${params}`, { method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: file, signal: AbortSignal.timeout(600_000) }, 600_000)
    resumeDraft.value = { name: file.name.replace(/\.pdf$/i, ''), source_text: documentPreview.value.extracted_text, source_file_name: file.name, document_id: documentPreview.value.id, is_active: true }
    showResumeForm.value = true; editingResume.value = true; resume.value = null; project.value = null; parseDraft.value = null; projects.value = []; versions.value = []
    notice.value = `PDF 提取完成：${documentPreview.value.page_count} 页。核对文字后再保存简历。`
  } catch (cause) { error.value = cause instanceof Error ? cause.message : 'PDF 识别失败。' }
  finally { busy.value = false }
}
async function reextract() {
  if (!resume.value?.document_id) return
  busy.value = true; error.value = ''
  try {
    documentPreview.value = await apiRequest<{ id: string; file_name: string; extracted_text: string; page_count: number; warnings: string[]; ocr_pages: number[] }>(`/materials/documents/${resume.value.document_id}/extract?ocr_mode=${ocrMode.value}`, { method: 'POST' }, 600_000)
    resumeDraft.value = { name: resume.value.name, source_text: documentPreview.value.extracted_text, source_file_name: resume.value.source_file_name, document_id: resume.value.document_id, is_active: resume.value.is_active }
    showResumeForm.value = true; editingResume.value = true
    notice.value = '重新提取结果是草稿；保存后才会形成新简历版本。'
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '重新识别失败。' }
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
  try { versionDetail.value = await apiRequest<{ version: number; source_text: string }>(`/materials/resumes/${resume.value.id}/versions/${version}`) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取版本失败。' }
}
async function openProject(item: Project) {
  if (projectFormOpen.value && !window.confirm('项目有未保存的修改，放弃并切换吗？')) return
  try {
    project.value = await apiRequest<Project>(`/materials/resumes/${resume.value?.id}/projects/${item.id}`)
    projectDraft.value = { name: project.value.name, summary: project.value.summary, role: project.value.role, duration: project.value.duration, tech_stack: project.value.tech_stack.join(', '), user_verified: project.value.user_verified, facts: structuredClone(project.value.facts) }
    projectFormOpen.value = false
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '读取项目失败。' }
}
function newProject() {
  project.value = null; candidateIndex.value = null; projectFormOpen.value = true
  projectCreateKey.value = crypto.randomUUID()
  projectDraft.value = { name: '', summary: '', role: '', duration: '', tech_stack: '', user_verified: false, facts: [{ fact_type: '职责', content: '', evidence_quote: '' }] }
}
function addFact(target: Fact[]) { target.push({ fact_type: '职责', content: '', evidence_quote: '' }) }
async function saveProject() {
  if (!resume.value) return
  projectBusy.value = true; error.value = ''
  const payload = { name: projectDraft.value.name, summary: projectDraft.value.summary, role: projectDraft.value.role, duration: projectDraft.value.duration, tech_stack: projectDraft.value.tech_stack.split(/[,，]/).map((item) => item.trim()).filter(Boolean), user_verified: projectDraft.value.user_verified, facts: projectDraft.value.facts.filter((item) => item.content.trim()).map(({ fact_type, content, evidence_quote }) => ({ fact_type, content, evidence_quote })) }
  try {
    if (candidateIndex.value !== null && parseDraft.value) {
      await apiRequest(`/materials/resumes/${resume.value.id}/parse-drafts/${parseDraft.value.id}/confirm`, postJson({ ...payload, candidate_index: candidateIndex.value, expected_revision: parseDraft.value.revision }))
      parseDraft.value = await apiRequest<ParseDraft>(`/materials/resumes/${resume.value.id}/parse-drafts/${parseDraft.value.id}`)
    } else if (project.value) {
      project.value = await apiRequest<Project>(`/materials/resumes/${resume.value.id}/projects/${project.value.id}`, putJson({ ...payload, expected_revision: project.value.revision }))
    } else {
      project.value = await apiRequest<Project>(`/materials/resumes/${resume.value.id}/projects`, withIdempotencyKey(postJson({ ...payload, source_version: selectedVersion.value }), projectCreateKey.value))
    }
    projectFormOpen.value = false; candidateIndex.value = null
    projects.value = (await apiRequest<{ items: Project[] }>(`/materials/resumes/${resume.value.id}/projects?limit=50&offset=0`)).items
    notice.value = '项目已保存；所依据的简历版本保持固定。'
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存项目失败。' }
  finally { projectBusy.value = false }
}
async function runExtraction() {
  if (!resume.value) return
  parseBusy.value = true; error.value = ''
  try {
    const signature = `${resume.value.id}:${selectedVersion.value}`
    let requestId = parseRequest.value?.signature === signature ? parseRequest.value.id : ''
    if (!requestId) {
      requestId = crypto.randomUUID()
      parseRequest.value = { signature, id: requestId }
    }
    parseDraft.value = await apiRequest<ParseDraft>(`/materials/resumes/${resume.value.id}/parse-drafts`, postJson({ request_id: requestId, source_version: selectedVersion.value }), 330_000)
    parseRequest.value = null
    if (parseDraft.value.status === 'failed') error.value = parseDraft.value.error_message || '提取失败。'
    else notice.value = 'AI 候选已生成。请载入候选、编辑并核对后再逐项保存。'
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '项目提取失败。' }
  finally { parseBusy.value = false }
}
function editCandidate(index: number) {
  const item = parseDraft.value?.result?.projects[index]
  if (!item) return
  candidateIndex.value = index; project.value = null; projectFormOpen.value = true
  projectDraft.value = { name: item.name, summary: item.summary ?? '', role: item.role ?? '', duration: item.duration ?? '', tech_stack: item.tech_stack.join(', '), user_verified: false, facts: structuredClone(item.facts) }
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
    <div class="page-heading"><div><p class="eyebrow">SOURCE BEFORE SUMMARY</p><h1>简历与项目</h1><p class="heading-intro">原件、提取草稿、已保存版本和项目事实各自保留，依据清晰可追。</p></div><div class="heading-actions"><label class="button button-outline file-button">上传 PDF<input type="file" accept="application/pdf,.pdf" @change="uploadPdf(($event.target as HTMLInputElement).files?.[0])" /></label><button class="button button-primary" @click="newResume()">＋ 新建简历</button></div></div>
    <div class="materials-layout">
      <aside class="materials-rail"><p class="eyebrow">SAVED RESUMES</p><button v-for="item in resumes" :key="item.id" class="resume-choice" :class="{ active: resume?.id === item.id }" @click="selectResume(item.id)"><span class="resume-symbol">R</span><span><strong>{{ item.name }}</strong><small>{{ item.is_active ? '使用中' : '已停用' }} · v{{ item.content_version }}</small></span></button><p v-if="!resumes.length" class="quiet-copy">保存简历后会显示在这里。</p><button class="text-action add-resume" @click="newResume()">＋ 新建资料</button><div v-if="resume" class="version-rail"><p class="eyebrow">VERSION HISTORY</p><button v-for="version in versions" :key="version.version" :class="{ current: version.version === resume.content_version }" @click="loadVersion(version.version)"><span>v{{ version.version }}</span><small>{{ version.name }}</small><i v-if="version.version === resume.content_version">当前</i></button></div></aside>
      <section class="materials-main">
        <div v-if="showResumeForm" class="resume-editor"><div class="section-title"><div><p class="eyebrow">{{ resume ? 'EDIT DRAFT' : 'NEW RESUME' }}</p><h2>{{ resume ? '核对并保存简历' : '建立一份简历版本' }}</h2></div><button class="text-action" @click="cancelResumeEdit()">取消</button></div><label class="editor-label">资料名称</label><input v-model="resumeDraft.name" class="material-input" maxlength="200" /><label class="verified-choice"><input v-model="resumeDraft.is_active" type="checkbox" /> 设为当前使用简历</label><div class="file-source" v-if="resumeDraft.document_id"><span>PDF 原件已关联 · {{ resumeDraft.source_file_name }}</span><a :href="`${legacyOrigin}/materials/documents/${resumeDraft.document_id}/file`" target="_blank">查看原件 ↗</a></div><div v-if="documentPreview" class="extract-summary"><b>{{ documentPreview.page_count }} 页</b><span>OCR 页面：{{ documentPreview.ocr_pages.length ? documentPreview.ocr_pages.join('、') : '无' }}</span><small v-for="warning in documentPreview.warnings" :key="warning">{{ warning }}</small></div><label class="editor-label">提取文字 / 简历正文</label><textarea v-model="resumeDraft.source_text" class="resume-text-editor" rows="18" placeholder="粘贴简历文字。原文会按版本保存。"></textarea><div class="editor-toolbar"><label>重新识别策略 <select v-model="ocrMode"><option value="auto">仅文字不足页 OCR</option><option value="force">整份 OCR</option><option value="off">关闭 OCR</option></select></label><button v-if="resume?.document_id" class="text-action" @click="reextract()">重新提取</button><button class="text-action" @click="formatText()">整理断行草稿</button><button class="button button-primary button-small" :disabled="busy" @click="saveResume()">{{ busy ? '保存中…' : '保存版本' }}</button></div></div>
        <template v-else-if="resume">
          <div class="material-overview"><div><p class="eyebrow">SAVED SOURCE · VERSION {{ resume.content_version }}</p><h2>{{ resume.name }}</h2><p>{{ resume.source_file_name || '手工文字资料' }} <span>·</span> 更新于 {{ new Date(resume.updated_at).toLocaleDateString('zh-CN') }}</p></div><div class="overview-actions"><button class="button button-outline button-small" @click="showResumeForm = true; editingResume = true">编辑文字</button><button class="text-action danger-action" @click="deleteResume()">移除</button></div></div>
          <div v-if="versionDetail" class="version-preview"><div><strong>简历 v{{ versionDetail.version }} 只读预览</strong><button class="text-action" @click="versionDetail = null">关闭</button></div><pre>{{ versionDetail.source_text }}</pre></div>
          <section class="project-workspace"><div class="section-title"><div><p class="eyebrow">PROJECTS / EVIDENCE</p><h2>项目事实 <small>依据 {{ currentSourceLabel }}</small></h2></div><div class="project-actions"><button class="button button-outline button-small" :disabled="parseBusy" @click="runExtraction()">{{ parseBusy ? '提取中…' : 'AI 提取草稿' }}</button><button class="button button-primary button-small" @click="newProject()">＋ 添加项目</button></div></div>
            <div v-if="parseDraft?.result" class="candidate-review"><div class="candidate-review-title"><span><b>AI 候选 · 待人工核对</b><small>读取已保存简历 v{{ parseDraft.source_version }}；模型置信度不代表真实性。</small></span><button class="text-action" @click="parseDraft = null">收起</button></div><article v-for="(candidate, index) in parseDraft.result.projects" :key="index" class="ai-candidate"><div><strong>{{ candidate.name }}</strong><span>模型置信 {{ Math.round(candidate.confidence * 100) }}% · 原文依据待核对</span><blockquote>{{ candidate.evidence_quote }}</blockquote></div><button v-if="parseDraft.confirmed_projects[String(index)]" class="status-chip chip-green">已确认</button><button v-else class="text-action" @click="editCandidate(index)">载入编辑核对 →</button></article></div>
            <div v-if="projectFormOpen" class="project-editor"><div class="editor-title"><b>{{ candidateIndex !== null ? '载入候选并人工核对' : project ? '编辑项目' : '新建项目' }}</b><button class="text-action" @click="projectFormOpen = false; candidateIndex = null">取消</button></div><div class="project-fields"><label>项目名称<input v-model="projectDraft.name" /></label><label>角色<input v-model="projectDraft.role" /></label><label>周期<input v-model="projectDraft.duration" /></label><label>技术栈<input v-model="projectDraft.tech_stack" placeholder="Python, FastAPI, ..." /></label><label class="full-field">项目摘要<textarea v-model="projectDraft.summary" rows="3"></textarea></label></div><div class="facts-editor"><div class="editor-title"><b>项目事实与依据</b><button class="text-action" @click="addFact(projectDraft.facts)">＋ 添加事实</button></div><article v-for="(fact, index) in projectDraft.facts" :key="index" class="fact-edit"><input v-model="fact.fact_type" aria-label="事实类型" placeholder="类型" /><textarea v-model="fact.content" aria-label="事实内容" placeholder="事实内容"></textarea><textarea v-model="fact.evidence_quote" aria-label="原文引用" placeholder="简历原文引用；无引用留空并标为手工补充"></textarea><button class="remove-fact" @click="projectDraft.facts.splice(index, 1)">×</button></article></div><label class="verified-choice"><input v-model="projectDraft.user_verified" type="checkbox" /> 我已核对并确认此项目</label><div class="editor-toolbar"><span>写入项目后固定引用简历 v{{ selectedVersion }}</span><button class="button button-primary button-small" :disabled="projectBusy" @click="saveProject()">{{ projectBusy ? '保存中…' : '保存项目' }}</button></div></div>
            <div class="project-list"><article v-for="item in projects" :key="item.id" class="project-card" :class="{ selected: project?.id === item.id }" @click="openProject(item)"><div class="project-card-head"><div><span class="project-state" :class="item.user_verified ? 'verified' : ''">{{ item.user_verified ? '已核对' : '待核对' }}</span><h3>{{ item.name }}</h3></div><span class="source-version">依据 v{{ item.source_version }}</span></div><p>{{ item.summary || '暂无项目摘要。' }}</p><div class="tech-tags"><span v-for="tech in item.tech_stack" :key="tech">{{ tech }}</span></div><div class="project-facts"><div v-for="fact in item.facts.slice(0, 3)" :key="fact.id"><b>{{ fact.fact_type }}</b><span>{{ fact.content }}</span><small>{{ fact.evidence_quote ? '简历原文引用' : '手工补充' }}</small></div><button class="text-action danger-action" @click.stop="deleteProject(item)">删除项目</button></div></article><div v-if="!projects.length" class="no-projects"><span>没有项目事实</span><p>添加已核对项目，或从当前保存版本提取候选后人工核对。</p><button class="text-action" @click="newProject()">添加第一个项目 →</button></div></div>
          </section>
        </template>
        <div v-else class="materials-empty"><p class="eyebrow">MATERIALS, WITH PROVENANCE</p><h2>先保留原件，<br />再核对事实。</h2><p>上传 PDF 或新建文字简历。提取只生成可编辑草稿，保存后才形成版本。</p><label class="button button-primary file-button">上传 PDF<input type="file" accept="application/pdf,.pdf" @change="uploadPdf(($event.target as HTMLInputElement).files?.[0])" /></label></div>
      </section>
    </div>
    <div v-if="busy || loading" class="busy-banner"><span class="spinner"></span>{{ notice || '正在处理资料…' }}</div>
    <div v-if="error || notice" class="toast" :class="{ 'toast-error': error }" role="status"><span>{{ error || notice }}</span><button @click="error = ''; notice = ''">×</button></div>
  </main>
</template>
