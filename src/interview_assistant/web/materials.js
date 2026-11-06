const $ = (id) => document.getElementById(id);
const base = "/materials/resumes";
const pageSize = 20;
const state = {
  busy: false, resume: null, editing: false, project: null, projectEditing: false,
  documentId: null, fileName: "", importedText: "", source: null,
  resumes: [], resumeTotal: 0, resumeOffset: 0, search: "",
  projects: [], projectTotal: 0, projectOffset: 0,
  versionTotal: 0, versionOffset: 0, tab: "resume", creation: {}, formatUndo: null,
};
const normalized = (value) => value.replace(/\r\n?/g, "\n");
const originalText = (value, original = "") => value === normalized(original) ? original : value;
const date = (value) => new Date(value).toLocaleString("zh-CN", { hour12: false });

class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

async function request(path, options = {}) {
  const { timeoutMs = 60000, ...fetchOptions } = options;
  let response;
  try {
    response = await fetch(path, {
      ...fetchOptions,
      headers: { Accept: "application/json", ...fetchOptions.headers },
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch {
    throw new ApiError("连接失败或请求超时，输入已保留。请确认服务状态后重试。", 0);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const fallback = {
      404: "资料已不存在，请刷新列表。", 409: "资料发生冲突，请保留输入并读取最新内容。",
      413: "文件过大，请精简后重试。", 422: "内容不符合要求，请检查必填项、字段长度与事实引用。",
      503: "服务暂不可用，请查看后端提示或确认依赖已安装。",
    };
    throw new ApiError(typeof detail?.message === "string" ? detail.message :
      fallback[response.status] || "操作未完成，输入已保留，请稍后重试。", response.status, detail?.code);
  }
  return response.status === 204 ? null : response.json();
}

function notify(message, error = false) {
  $("message-text").textContent = message;
  $("message").classList.toggle("error", error);
  $("message").hidden = false;
}

function report(error) {
  notify(error instanceof ApiError ? error.message : "操作未完成，输入已保留，请重试。", true);
  if (error.code === "revision_conflict") {
    $(state.tab === "projects" ? "project-conflict" : "resume-conflict").hidden = false;
  }
}

function element(tag, text, className) {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function pdfLink(id, documentId) {
  const link = $(id);
  link.hidden = !documentId;
  if (documentId) link.href = `/materials/documents/${encodeURIComponent(documentId)}/file`;
  else link.removeAttribute("href");
}

function resumeValues() {
  return {
    name: $("resume-name").value,
    source_text: originalText($("resume-text").value, state.importedText),
    source_file_name: state.fileName,
    document_id: state.documentId,
    is_active: $("resume-active").checked,
  };
}

function resumeBaseline() {
  const resume = state.resume;
  return resume ? {
    name: resume.name, source_text: resume.source_text, source_file_name: resume.source_file_name,
    document_id: resume.document_id, is_active: resume.is_active,
  } : { name: "", source_text: "", source_file_name: "", document_id: null, is_active: true };
}

function factValues(card) {
  const previous = card.original || {};
  return {
    fact_type: card.querySelector("[data-fact-type]").value,
    content: originalText(card.querySelector("[data-fact-content]").value, previous.content),
    evidence_quote: originalText(card.querySelector("[data-fact-evidence]").value, previous.evidence_quote),
  };
}

function projectValues() {
  const stack = $("project-stack").value;
  const previous = state.project?.tech_stack || [];
  return {
    name: $("project-name").value,
    summary: originalText($("project-summary").value, state.project?.summary),
    role: $("project-role").value, duration: $("project-duration").value,
    tech_stack: stack === previous.join(", ") ? previous : stack.split(/[,，]/).map((v) => v.trim()).filter(Boolean),
    user_verified: $("project-verified").checked,
    facts: [...$("fact-list").children].map(factValues),
  };
}

function projectBaseline() {
  const project = state.project;
  return project ? {
    name: project.name, summary: project.summary, role: project.role, duration: project.duration,
    tech_stack: project.tech_stack, user_verified: project.user_verified,
    facts: project.facts.map(({ fact_type, content, evidence_quote }) => ({ fact_type, content, evidence_quote })),
  } : { name: "", summary: "", role: "", duration: "", tech_stack: [], user_verified: false, facts: [] };
}

const resumeDirty = () => state.editing && JSON.stringify(resumeValues()) !== JSON.stringify(resumeBaseline());
const projectDirty = () => state.projectEditing && JSON.stringify(projectValues()) !== JSON.stringify(projectBaseline());
function canLeave(projectOnly = false) {
  const dirty = projectDirty() || (!projectOnly && resumeDirty());
  return !dirty || window.confirm("有尚未保存的修改，是否放弃这些修改？");
}

function updateControls() {
  $("save-state").textContent = state.busy ? "正在处理…" : resumeDirty() ? "简历有未保存修改" : state.resume ? `简历 v${state.resume.content_version}` : "尚未保存";
  $("project-save-state").textContent = projectDirty() ? "有未保存的修改" : state.project ? "已保存" : "尚未保存";
  $("save-resume").disabled = !!state.resume && !resumeDirty();
  $("save-project").disabled = !!state.project && !projectDirty();
  $("tab-projects").disabled = !state.resume;
  $("tab-history").disabled = !state.resume;
  $("delete-resume").hidden = !state.resume;
  $("delete-project").hidden = !state.project;
  $("reextract-pdf").hidden = !state.documentId;
  $("format-resume-text").disabled = !$("resume-text").value.trim();
  $("undo-text-format").hidden = state.formatUndo === null;
}

async function operate(action) {
  if (state.busy) return;
  state.busy = true;
  $("main").inert = true;
  $("main").setAttribute("aria-busy", "true");
  updateControls();
  try { await action(); } catch (error) { report(error); }
  finally {
    state.busy = false;
    $("main").inert = false;
    $("main").setAttribute("aria-busy", "false");
    updateControls();
  }
}

function on(id, event, action) {
  $(id).addEventListener(event, (e) => {
    if (event === "submit") e.preventDefault();
    void operate(() => action(e));
  });
}

function pageControls(prefix, total, offset) {
  $(`${prefix}-page`).textContent = `${Math.floor(offset / pageSize) + 1} / ${Math.max(1, Math.ceil(total / pageSize))}`;
  $(`${prefix}-previous`).disabled = offset === 0;
  $(`${prefix}-next`).disabled = offset + pageSize >= total;
}

async function loadResumes() {
  const params = new URLSearchParams({ limit: pageSize, offset: state.resumeOffset });
  if (state.search) params.set("search", state.search);
  try {
    const data = await request(`${base}?${params}`);
    state.resumes = data.items;
    state.resumeTotal = data.total;
    $("resume-count").textContent = data.total;
    renderResumes();
    $("connection").className = "connection ok";
    $("connection").textContent = "资料已连接";
  } catch (error) {
    $("connection").className = "connection error";
    $("connection").textContent = "连接异常";
    $("resume-list").replaceChildren(element("p", "读取失败，点击刷新重试。", "list-placeholder"));
    $("resume-previous").disabled = true;
    $("resume-next").disabled = true;
    throw error;
  }
}

function renderResumes() {
  $("resume-list").replaceChildren();
  if (!state.resumes.length) $("resume-list").append(element("p", state.search ? "没有找到对应简历。" : "还没有简历，点击「新建简历」开始。", "list-placeholder"));
  for (const resume of state.resumes) {
    const button = element("button", "", `question-card${state.resume?.id === resume.id ? " selected" : ""}`);
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.resume?.id === resume.id));
    button.append(element("div", `${resume.is_active ? "可用于面试" : "已停用"} · v${resume.content_version}`, "card-meta"),
      element("h3", resume.name, "card-title"),
      element("p", resume.source_file_name || "手工录入", "card-snippet"));
    button.addEventListener("click", () => void operate(() => selectResume(resume.id)));
    $("resume-list").append(button);
  }
  pageControls("resume", state.resumeTotal, state.resumeOffset);
}

function showTab(tab) {
  state.tab = tab;
  for (const name of ["resume", "projects", "history"]) {
    $(`panel-${name}`).hidden = name !== tab;
    $(`tab-${name}`).setAttribute("aria-selected", String(name === tab));
  }
}

function fillResume(resume) {
  state.formatUndo = null;
  state.resume = resume;
  state.editing = true;
  state.documentId = resume?.document_id || null;
  state.fileName = resume?.source_file_name || "";
  state.importedText = resume?.source_text || "";
  $("resume-name").value = resume?.name || "";
  $("resume-text").value = state.importedText;
  $("resume-active").checked = resume?.is_active ?? true;
  $("resume-file").value = "";
  $("resume-file-info").textContent = state.fileName || "手工录入";
  $("extraction-warnings").hidden = true;
  $("resume-conflict").hidden = true;
  $("workspace-title").textContent = resume?.name || "新建简历";
  $("materials-workspace").hidden = false;
  $("materials-welcome").hidden = true;
  pdfLink("resume-pdf", state.documentId);
  resetProject();
  state.projectOffset = state.versionOffset = 0;
  $("version-detail").hidden = true;
  showTab("resume");
  renderResumes();
}

function resetProject() {
  state.project = null;
  state.projectEditing = false;
  state.source = null;
  $("project-form").hidden = true;
  $("project-welcome").hidden = false;
  $("project-conflict").hidden = true;
}

async function selectResume(id) {
  if (!canLeave()) return;
  const resume = await request(`${base}/${id}`);
  state.creation = {};
  fillResume(resume);
}

function creationKey(kind, payload) {
  const body = JSON.stringify(payload);
  if (state.creation[kind]?.body !== body) state.creation[kind] = { body, key: crypto.randomUUID() };
  return state.creation[kind].key;
}

function jsonOptions(method, payload, key) {
  return {
    method, body: JSON.stringify(payload),
    headers: { "Content-Type": "application/json", ...(key ? { "Idempotency-Key": key } : {}) },
  };
}

async function saveResume() {
  const payload = resumeValues();
  if (!payload.name.trim() || !payload.source_text.trim()) {
    notify("请填写简历名称和文字；扫描件可先手工粘贴文字。", true);
    return;
  }
  const current = state.resume;
  if (current) payload.expected_revision = current.revision;
  const result = await request(current ? `${base}/${current.id}` : base,
    jsonOptions(current ? "PUT" : "POST", payload, current ? null : creationKey("resume", payload)));
  // 保存简历时，保留另一个标签里尚未保存的项目草稿。
  state.resume = result;
  state.formatUndo = null;
  state.importedText = result.source_text;
  state.fileName = result.source_file_name;
  state.documentId = result.document_id;
  $("workspace-title").textContent = result.name;
  $("resume-conflict").hidden = true;
  delete state.creation.resume;
  notify(`简历已保存，当前版本 v${result.content_version}。`);
  await loadResumes().catch(() => notify("简历已保存，但列表刷新失败，可稍后点击刷新。", true));
}

async function loadFile() {
  const file = $("resume-file").files[0];
  if (!file) return;
  if ($("resume-text").value && !window.confirm("载入文件会替换当前简历文字，是否继续？")) {
    $("resume-file").value = "";
    return;
  }
  const isPdf = /\.pdf$/i.test(file.name);
  const max = (isPdf ? 10 : 1) * 1024 * 1024;
  if (!/\.(pdf|md|markdown|txt)$/i.test(file.name) || file.size > max || file.size === 0) {
    notify("请选择 PDF（最多 10 MiB）或 UTF-8 文字文件（最多 1 MiB），文件不能为空。", true);
    $("resume-file").value = "";
    return;
  }
  let content;
  let documentId = null;
  let fileName = file.name;
  let warnings = "";
  if (isPdf) {
    const mode = $("pdf-ocr-mode").value;
    notify(mode === "off" ? "正在提取 PDF 文字…" : "正在提取文字并按所选方式进行 OCR；首次识别可能需要下载模型，请稍候。");
    const document = await request(`/materials/documents/pdf?file_name=${encodeURIComponent(file.name)}&ocr_mode=${mode}`, {
      method: "POST", headers: { "Content-Type": "application/pdf" }, body: file, timeoutMs: 300000,
    });
    content = document.extracted_text;
    documentId = document.id;
    fileName = document.file_name;
    warnings = extractionNotice(document);
  } else {
    try {
      content = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer());
    } catch {
      notify("文字文件不是有效 UTF-8，请另存为 UTF-8 后重新载入。", true);
      $("resume-file").value = "";
      return;
    }
  }
  state.documentId = documentId;
  state.formatUndo = null;
  state.fileName = fileName;
  state.importedText = content;
  $("resume-text").value = content;
  if (!$("resume-name").value.trim()) $("resume-name").value = file.name.replace(/\.[^.]+$/, "").slice(0, 200);
  $("resume-file-info").textContent = fileName;
  $("extraction-warnings").textContent = warnings;
  $("extraction-warnings").hidden = !warnings;
  pdfLink("resume-pdf", documentId);
  $("resume-file").value = "";
  notify(isPdf ? "PDF 已载入，请核对并保存简历文字。" : "文字已载入，点击保存简历后生效。");
}

function extractionNotice(document) {
  const recognized = document.ocr_pages.length ? `其中 ${document.ocr_pages.length} 页经 OCR 识别。` : "";
  return [`已保存 ${document.page_count} 页 PDF 原件。${recognized}点击保存后才创建或更新简历。`, ...document.warnings].join("\n");
}

async function reextractPdf() {
  if (!state.documentId) return;
  if ($("resume-text").value && !window.confirm("重新识别会替换当前文字；已保存版本保留。是否继续？")) return;
  notify("正在重新识别 PDF，首次 OCR 可能需要下载模型，请稍候。");
  const result = await request(`/materials/documents/${state.documentId}/extract?ocr_mode=${$("pdf-ocr-mode").value}`, {
    method: "POST", timeoutMs: 300000,
  });
  state.importedText = result.extracted_text;
  state.formatUndo = null;
  $("resume-text").value = result.extracted_text;
  $("extraction-warnings").textContent = extractionNotice(result);
  $("extraction-warnings").hidden = false;
  notify("识别结果已载入，请核对后点击保存简历。");
}

async function formatResumeText() {
  const original = $("resume-text").value;
  if (!original.trim()) return;
  const result = await request("/materials/text/format", jsonOptions("POST", { source_text: original }));
  if (result.source_text === original) {
    notify("当前文字没有可整理的明显断行；错字和缺失内容请对照 PDF 原件修正。");
    return;
  }
  state.formatUndo = original;
  $("resume-text").value = result.source_text;
  notify("已整理换行与分段，可撤销。请核对后保存；杂字符和缺失内容未自动改写。");
}

async function loadProjects() {
  const data = await request(`${base}/${state.resume.id}/projects?limit=${pageSize}&offset=${state.projectOffset}`);
  state.projects = data.items;
  state.projectTotal = data.total;
  $("project-count").textContent = `(${data.total})`;
  renderProjects();
}

function renderProjects() {
  $("project-list").replaceChildren();
  if (!state.projects.length) $("project-list").append(element("p", "这里还没有项目，先整理一个熟悉的项目。", "material-help"));
  for (const project of state.projects) {
    const button = element("button", "", `project-card${state.project?.id === project.id ? " selected" : ""}`);
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.project?.id === project.id));
    button.append(element("strong", project.name), element("small", `${project.user_verified ? "已核对" : "待核对"} · 简历 v${project.source_version} · ${project.facts.length} 条事实`));
    button.addEventListener("click", () => void operate(() => selectProject(project.id)));
    $("project-list").append(button);
  }
  pageControls("project", state.projectTotal, state.projectOffset);
}

function addFact(fact = { fact_type: "职责", content: "", evidence_quote: "" }) {
  const card = element("section", "", "fact-card");
  card.original = fact;
  const tools = element("div", "", "project-toolbar");
  const source = element("span", fact.evidence_quote ? "简历原文引用" : "手工补充");
  source.dataset.factSource = "";
  const remove = element("button", "移除", "text-button danger");
  remove.type = "button";
  remove.addEventListener("click", () => {
    if (state.busy) return;
    card.remove();
    $("project-verified").checked = false;
    updateControls();
  });
  tools.append(source, remove);
  card.append(tools);
  for (const [key, label, attribute, rows, maxLength] of [
    ["fact_type", "事实类型", "data-fact-type", 0, 50],
    ["content", "事实内容", "data-fact-content", 3, 10000],
    ["evidence_quote", "简历原文引用（选填）", "data-fact-evidence", 3, 20000],
  ]) {
    const field = element("div", "", "field");
    const input = document.createElement(rows ? "textarea" : "input");
    const id = `fact-${crypto.randomUUID()}`;
    input.id = id;
    input.value = fact[key];
    input.maxLength = maxLength;
    input.setAttribute(attribute, "");
    if (rows) input.rows = rows;
    if (key !== "evidence_quote") input.required = true;
    if (key === "evidence_quote") input.placeholder = "从上方项目来源文字中复制连续原文；不填写则记为手工补充。";
    const title = element("label", label);
    title.htmlFor = id;
    field.append(title, input);
    card.append(field);
  }
  $("fact-list").append(card);
}

function fillProject(project, source) {
  state.project = project;
  state.projectEditing = true;
  state.source = source;
  $("project-form").hidden = false;
  $("project-welcome").hidden = true;
  $("project-title").textContent = project ? "编辑项目" : "新建项目";
  $("project-name").value = project?.name || "";
  $("project-summary").value = project?.summary || "";
  $("project-role").value = project?.role || "";
  $("project-duration").value = project?.duration || "";
  $("project-stack").value = (project?.tech_stack || []).join(", ");
  $("project-verified").checked = project?.user_verified || false;
  $("fact-list").replaceChildren();
  for (const fact of project?.facts || []) addFact(fact);
  $("project-source-label").textContent = `来源：${source.name} · 简历 v${source.version}。更新简历不会自动切换此项目的来源版本。`;
  $("project-source-text").textContent = source.source_text;
  pdfLink("project-source-pdf", source.document_id);
  $("project-conflict").hidden = true;
  renderProjects();
}

async function selectProject(id) {
  if (!canLeave(true)) return;
  const project = await request(`${base}/${state.resume.id}/projects/${id}`);
  const source = await request(`${base}/${state.resume.id}/versions/${project.source_version}`);
  delete state.creation.project;
  fillProject(project, source);
}

async function saveProject() {
  const payload = projectValues();
  if (!payload.name.trim() || payload.facts.some((fact) => !fact.content.trim() || !fact.fact_type.trim())) {
    notify("请填写项目名称和每条事实的类型、内容，或移除空白事实。", true);
    return;
  }
  const project = state.project;
  if (project) payload.expected_revision = project.revision;
  else payload.source_version = state.source.version;
  const path = `${base}/${state.resume.id}/projects`;
  const result = await request(project ? `${path}/${project.id}` : path,
    jsonOptions(project ? "PUT" : "POST", payload, project ? null : creationKey("project", payload)));
  fillProject(result, state.source);
  delete state.creation.project;
  notify("项目与事实已保存。");
  await loadProjects().catch(() => notify("项目已保存，但列表刷新失败，可重新打开项目标签。", true));
}

async function loadVersions() {
  const data = await request(`${base}/${state.resume.id}/versions?limit=${pageSize}&offset=${state.versionOffset}`);
  state.versionTotal = data.total;
  $("version-list").replaceChildren();
  for (const version of data.items) {
    const button = element("button", `v${version.version} · ${version.name} · ${date(version.created_at)}`, "project-card");
    button.type = "button";
    button.addEventListener("click", () => void operate(async () => {
      const detail = await request(`${base}/${state.resume.id}/versions/${version.version}`);
      $("version-title").textContent = `v${detail.version} · ${detail.source_file_name || "手工录入"}`;
      $("version-text").textContent = detail.source_text;
      pdfLink("version-pdf", detail.document_id);
      $("version-detail").hidden = false;
    }));
    $("version-list").append(button);
  }
  pageControls("version", data.total, state.versionOffset);
}

on("new-resume", "click", () => { if (canLeave()) { state.creation = {}; fillResume(null); } });
on("welcome-new", "click", () => { state.creation = {}; fillResume(null); });
on("refresh-resumes", "click", loadResumes);
on("dismiss-message", "click", () => { $("message").hidden = true; });
on("resume-form", "submit", saveResume);
on("reextract-pdf", "click", reextractPdf);
on("format-resume-text", "click", formatResumeText);
on("undo-text-format", "click", () => {
  if (state.formatUndo === null) return;
  $("resume-text").value = state.formatUndo;
  state.formatUndo = null;
  notify("已恢复整理前的文字。");
});
on("resume-file", "change", async () => {
  try { await loadFile(); } finally { $("resume-file").value = ""; }
});
on("reload-resume", "click", () => state.resume && selectResume(state.resume.id));
on("delete-resume", "click", async () => {
  if (!state.resume || !window.confirm("删除这份简历？关联项目将一并从工作区隐藏，历史和原件保留。")) return;
  await request(`${base}/${state.resume.id}?expected_revision=${state.resume.revision}`, { method: "DELETE" });
  state.resume = null;
  state.editing = false;
  resetProject();
  state.creation = {};
  $("materials-workspace").hidden = true;
  $("materials-welcome").hidden = false;
  notify("简历已删除。");
  state.resumeOffset = 0;
  await loadResumes();
});
on("resume-search-form", "submit", async () => { state.search = $("resume-search").value; state.resumeOffset = 0; await loadResumes(); });
on("resume-previous", "click", async () => { state.resumeOffset = Math.max(0, state.resumeOffset - pageSize); await loadResumes(); });
on("resume-next", "click", async () => { state.resumeOffset += pageSize; await loadResumes(); });
on("tab-resume", "click", () => showTab("resume"));
on("tab-projects", "click", async () => { if (state.resume) { await loadProjects(); showTab("projects"); } });
on("tab-history", "click", async () => { if (state.resume) { await loadVersions(); showTab("history"); } });
on("new-project", "click", async () => {
  if (!canLeave(true)) return;
  const source = await request(`${base}/${state.resume.id}/versions/${state.resume.content_version}`);
  delete state.creation.project;
  fillProject(null, source);
});
on("project-form", "submit", saveProject);
on("add-fact", "click", () => {
  if ($("fact-list").children.length >= 100) { notify("每个项目最多保存 100 条事实。", true); return; }
  addFact();
  $("project-verified").checked = false;
});
on("reload-project", "click", () => state.project && selectProject(state.project.id));
on("delete-project", "click", async () => {
  if (!state.project || !window.confirm("删除这个项目？已保存的事实与来源记录将保留。")) return;
  await request(`${base}/${state.resume.id}/projects/${state.project.id}?expected_revision=${state.project.revision}`, { method: "DELETE" });
  resetProject();
  delete state.creation.project;
  state.projectOffset = 0;
  notify("项目已删除。");
  await loadProjects();
});
for (const [prefix, loader] of [["project", loadProjects], ["version", loadVersions]]) {
  on(`${prefix}-previous`, "click", async () => { state[`${prefix}Offset`] = Math.max(0, state[`${prefix}Offset`] - pageSize); await loader(); });
  on(`${prefix}-next`, "click", async () => { state[`${prefix}Offset`] += pageSize; await loader(); });
}
$("resume-form").addEventListener("input", updateControls);
$("resume-text").addEventListener("input", () => { state.formatUndo = null; updateControls(); });
$("project-form").addEventListener("input", (event) => {
  if (event.target !== $("project-verified")) $("project-verified").checked = false;
  for (const card of $("fact-list").children) {
    card.querySelector("[data-fact-source]").textContent = card.querySelector("[data-fact-evidence]").value ? "简历原文引用" : "手工补充";
  }
  updateControls();
});
for (const id of ["resume-form", "project-form"]) {
  $(id).addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      if (!state.busy) $(id).requestSubmit();
    }
  });
}
window.addEventListener("beforeunload", (event) => {
  if (state.busy || resumeDirty() || projectDirty()) { event.preventDefault(); event.returnValue = ""; }
});

await operate(loadResumes);
