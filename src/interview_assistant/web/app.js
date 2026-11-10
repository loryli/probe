import { renderRecord } from "./record_view.js?v=20261110-4";
import { setupMarkdownImport } from "./imports.js";

const $ = (id) => document.getElementById(id);
const base = "/question-bank/questions";
const pageSize = 20;
const textFields = ["question_text", "standard_answer_text", "user_notes"];
const inputs = {
  question_text: $("question-text"),
  standard_answer_text: $("answer-text"),
  user_notes: $("user-notes"),
};
const state = {
  selected: null, isNew: false, editing: false, busy: false, offset: 0, total: 0,
  search: "", filter: "", items: [], listRequest: 0, listLoading: false, listError: false,
  category: "", categories: [], categoryCounts: null, checked: new Set(),
  categoryRequest: 0, categoryEditing: null, categoryCreation: null, categoryBusy: false, moveBusy: false,
  historyOffset: 0, historyRequest: 0, tab: "content", creation: null,
};
const dateFormat = new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
});

class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers },
      signal: AbortSignal.timeout(15000),
    });
  } catch {
    throw new ApiError("连接失败或请求超时。请确认后端正在运行；当前输入已保留，可以重试。", 0);
  }
  if (!response.ok) {
    const messages = {
      404: "这道题目已不存在或已被删除。请刷新题目列表。",
      409: "题目已被其他操作修改，请读取最新内容后再保存。当前输入已保留。",
      422: "输入未通过校验，请检查题目是否为空，以及填写内容是否正确。",
      503: "数据库暂时不可用，请检查后端状态后重试。当前输入已保留。",
    };
    const data = await response.json().catch(() => null);
    const detail = data?.detail;
    const message = typeof detail?.message === "string" ? detail.message : messages[response.status];
    throw new ApiError(message || "操作未成功，请稍后重试。当前输入已保留。", response.status, detail?.code);
  }
  return response.status === 204 ? null : response.json();
}

function notify(text, error = false) {
  $("message-text").textContent = text;
  $("message").classList.toggle("error", error);
  $("message").hidden = false;
}

function report(error) {
  notify(error instanceof ApiError ? error.message : "操作未完成，请重试。当前输入已保留。", true);
  if (error instanceof ApiError && error.code === "revision_conflict" && state.selected) {
    $("conflict-actions").hidden = false;
  }
}

function on(id, event, action) {
  $(id).addEventListener(event, async (e) => {
    try { await action(e); } catch (error) { report(error); }
  });
}

// Browsers normalize textarea newlines. Compare normalized values, but send only
// fields actually edited so an untouched Markdown answer keeps its original CRLF.
const normalized = (text) => text.replace(/\r\n?/g, "\n");
function values() {
  return {
    question_text: inputs.question_text.value,
    standard_answer_text: inputs.standard_answer_text.value,
    user_notes: inputs.user_notes.value,
    is_active: $("is-active").checked,
    category_id: $("question-category").value || null,
  };
}

function changes() {
  const current = values();
  const previous = state.selected || { question_text: "", standard_answer_text: "", user_notes: "", is_active: true, category_id: null };
  return Object.fromEntries(Object.entries(current).filter(([key, value]) => (
    textFields.includes(key) ? value !== normalized(previous[key]) : value !== (previous[key] ?? null)
  )));
}

function isDirty() {
  return state.editing && (state.isNew || state.selected !== null) && Object.keys(changes()).length > 0;
}

function canLeave() {
  return !state.busy && (!isDirty() || window.confirm("有尚未保存的内容，是否放弃这些修改？"));
}

function updateSaveState() {
  $("question-form").hidden = !state.editing;
  $("question-record-view").hidden = state.editing || !state.selected;
  $("edit-question").hidden = state.editing || !state.selected;
  $("cancel-question").hidden = !state.editing;
  $("save-question").hidden = !state.editing;
  $("question-mode").textContent = state.editing ? (state.isNew ? "新建题目" : "编辑题目") : "查看题目";
  if (!state.editing && state.selected) {
    const category = state.categories.find((item) => item.id === state.selected.category_id);
    renderRecord($("question-record-view"), {
      title: state.selected.question_text,
      meta: `${category?.name || "未分类"} · ${state.selected.is_active ? "已启用" : "已停用"} · 原文 v${state.selected.content_version}`,
      fields: [["标准答案", state.selected.standard_answer_text], ["复习笔记", state.selected.user_notes]],
    });
  }
  $("save-state").textContent = state.busy ? "正在处理…" : isDirty() ? "有未保存的修改" : state.isNew ? "尚未保存" : "已保存";
  $("save-question").disabled = state.busy || (!state.isNew && !isDirty());
}

function setBusy(busy) {
  state.busy = busy;
  for (const input of Object.values(inputs)) input.disabled = busy;
  for (const id of ["edit-question", "cancel-question", "is-active", "question-category", "new-category", "new-question", "import-markdown", "delete-question", "reload-current", "search", "active-filter", "refresh"]) {
    $(id).disabled = busy;
  }
  $("history-tab").disabled = busy || state.isNew || !state.selected;
  $("source-tab").disabled = busy || state.selected?.source !== "markdown";
  $("content-tab").disabled = busy;
  renderList();
  renderCategories();
  updateSaveState();
}

function text(tag, content, className) {
  const element = document.createElement(tag);
  element.textContent = content;
  if (className) element.className = className;
  return element;
}

function renderList() {
  const list = $("question-list");
  list.replaceChildren();
  list.setAttribute("aria-busy", String(state.listLoading));
  if (state.listError) {
    list.append(text("p", "读取失败，点击上方「刷新」重试。", "list-placeholder"));
  } else if (!state.items.length) {
    list.append(text("p", state.listLoading ? "正在读取题库…" : state.search || state.filter || state.category ? "没有找到符合条件的题目，试试其他筛选条件。" : "这里还没有题目。\n点击「新建题目」开始整理。", "list-placeholder"));
  }
  for (const item of state.listError ? [] : state.items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `question-card${state.selected?.id === item.id ? " selected" : ""}`;
    button.setAttribute("aria-pressed", String(state.selected?.id === item.id));
    button.disabled = state.busy || state.listLoading;
    const meta = text("div", "", "card-meta");
    meta.append(text("span", item.is_active ? "已启用" : "已停用", `status-label${item.is_active ? "" : " inactive"}`));
    meta.append(text("span", `原文 v${item.content_version}`));
    button.append(meta, text("h3", item.question_text, "card-title"), text("p", item.standard_answer_text || "还没有标准答案", "card-snippet"));
    button.addEventListener("click", () => selectQuestion(item.id).catch(report));
    const row = text("div", "", "question-row");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.checked.has(item.id);
    checkbox.disabled = state.busy || state.listLoading;
    checkbox.setAttribute("aria-label", `选择题目：${item.question_text.slice(0, 60)}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.checked.add(item.id); else state.checked.delete(item.id);
      renderBatchControls();
    });
    const categoryName = state.categories.find((category) => category.id === item.category_id)?.name;
    button.append(text("span", categoryName || "未分类", "question-category-label"));
    row.append(checkbox, button);
    list.append(row);
  }
  renderBatchControls();
  $("page-number").textContent = `第 ${Math.floor(state.offset / pageSize) + 1} / ${Math.max(1, Math.ceil(state.total / pageSize))} 页`;
  $("previous-page").disabled = state.busy || state.listLoading || state.listError || state.offset === 0;
  $("next-page").disabled = state.busy || state.listLoading || state.listError || state.offset + pageSize >= state.total;
}

async function loadList() {
  const sequence = ++state.listRequest;
  state.listLoading = true;
  state.checked.clear();
  state.listError = false;
  renderList();
  const params = new URLSearchParams({ limit: String(pageSize), offset: String(state.offset) });
  if (state.search) params.set("search", state.search);
  if (state.filter) params.set("is_active", state.filter);
  if (state.category === "uncategorized") params.set("uncategorized", "true");
  else if (state.category) params.set("category_id", state.category);
  try {
    const data = await request(`${base}?${params}`);
    if (sequence !== state.listRequest) return false;
    if (data.total > 0 && state.offset >= data.total) {
      state.offset = Math.floor((data.total - 1) / pageSize) * pageSize;
      return await loadList();
    }
    state.total = data.total;
    state.items = data.items;
    $("total-count").textContent = `${data.total}`;
    // Category counts describe the whole bank; the list count follows filters.
    $("nav-count").textContent = state.categoryCounts?.total_questions ?? data.total;
    return true;
  } catch (error) {
    if (sequence === state.listRequest) {
      report(error);
      state.listError = true;
    }
    return false;
  } finally {
    if (sequence === state.listRequest) {
      state.listLoading = false;
      renderList();
    }
  }
}

function showContent() {
  state.tab = "content";
  ++state.historyRequest;
  $("content-panel").hidden = false;
  $("history-panel").hidden = true;
  $("source-panel").hidden = true;
  $("content-tab").setAttribute("aria-selected", "true");
  $("history-tab").setAttribute("aria-selected", "false");
  $("source-tab").setAttribute("aria-selected", "false");
}

function populate(question) {
  state.selected = question;
  state.isNew = question === null;
  state.editing = question === null;
  state.creation = null;
  ++state.historyRequest;
  for (const name of textFields) inputs[name].value = question?.[name] || "";
  $("is-active").checked = question?.is_active ?? true;
  categoryOptions($("question-category"), question ? question.category_id || "" : actualCategory());
  $("welcome").hidden = true;
  $("editor").hidden = false;
  document.querySelector(".editor-column").scrollTop = 0;
  $("editor-title").textContent = question ? "整理题目" : "新建题目";
  $("editor-kicker").textContent = question ? `QUESTION · ${question.id.slice(0, 8).toUpperCase()}` : "A NEW START";
  $("delete-question").hidden = !question;
  $("history-tab").disabled = !question;
  $("source-tab").disabled = question?.source !== "markdown";
  $("version-count").textContent = question ? String(question.content_version) : "";
  $("conflict-actions").hidden = true;
  showContent();
  renderList();
  updateSaveState();
}

function newQuestion() {
  if (!canLeave()) return;
  populate(null);
  $("message").hidden = true;
  inputs.question_text.focus();
  $("editor").scrollIntoView({ block: "nearest" });
}

async function selectQuestion(id) {
  if (!canLeave()) return;
  setBusy(true);
  try {
    const question = await request(`${base}/${encodeURIComponent(id)}`);
    populate(question);
    $("message").hidden = true;
    $("editor").scrollIntoView({ block: "nearest" });
  } finally { setBusy(false); }
}

async function saveQuestion(event) {
  event.preventDefault();
  if (state.busy || !state.editing) return;
  if (!inputs.question_text.value.trim()) {
    notify("请填写题目，题目不能只包含空白字符。", true);
    inputs.question_text.focus();
    return;
  }
  const creating = state.isNew;
  const payload = creating ? values() : { expected_revision: state.selected.revision, ...changes() };
  if (!creating && !isDirty()) return;
  const body = JSON.stringify(payload);
  if (creating && state.creation?.body !== body) state.creation = { body, key: crypto.randomUUID() };
  setBusy(true);
  try {
    const saved = await request(creating ? base : `${base}/${state.selected.id}`, {
      method: creating ? "POST" : "PATCH", body,
      headers: creating ? { "Idempotency-Key": state.creation.key } : {},
    });
    populate(saved);
    if (creating) {
      state.category = saved.category_id || "uncategorized";
      state.offset = 0;
      state.search = "";
      state.filter = "";
      $("search").value = "";
      $("active-filter").value = "";
    }
    await loadCategories();
    const refreshed = await loadList();
    if (refreshed) notify(creating ? "题目已创建，原文已保存。" : "修改已保存。内容变化可在「历史版本」查看。");
  } finally { setBusy(false); }
}

async function deleteQuestion() {
  if (state.busy || !state.selected) return;
  if (!window.confirm("确定删除这道题目？删除后它不再出现在题库中，历史记录仍会保留。" + (isDirty() ? "尚未保存的修改将被放弃。" : ""))) return;
  setBusy(true);
  try {
    await request(`${base}/${state.selected.id}?expected_revision=${state.selected.revision}`, { method: "DELETE" });
    state.selected = null;
    state.isNew = false;
    ++state.historyRequest;
    $("editor").hidden = true;
    $("welcome").hidden = false;
    await loadCategories();
    if (await loadList()) notify("题目已删除，历史记录已保留。");
  } finally { setBusy(false); }
}

function showVersion(version) {
  $("version-detail").hidden = false;
  $("version-title").textContent = `原文版本 ${version.version} · ${dateFormat.format(new Date(version.created_at))}`;
  $("version-question").textContent = version.question_text;
  $("version-answer").textContent = version.standard_answer_text || "（未填写）";
  $("version-notes").textContent = version.user_notes || "（未填写）";
  for (const button of $("history-list").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.version === String(version.version)));
  }
}

async function loadHistory() {
  if (!state.selected || state.busy) return;
  const id = state.selected.id;
  const sequence = ++state.historyRequest;
  $("version-detail").hidden = true;
  $("history-list").replaceChildren(text("p", "正在读取历史…", "panel-description"));
  $("history-previous").disabled = true;
  $("history-next").disabled = true;
  try {
    const data = await request(`${base}/${id}/versions?limit=${pageSize}&offset=${state.historyOffset}`);
    if (sequence !== state.historyRequest || state.selected?.id !== id || state.tab !== "history") return;
    $("history-list").replaceChildren();
    for (const version of data.items) {
      const button = text("button", `原文 v${version.version}`, "version-button");
      button.type = "button";
      button.dataset.version = String(version.version);
      button.append(text("small", dateFormat.format(new Date(version.created_at))));
      button.addEventListener("click", () => showVersion(version));
      $("history-list").append(button);
    }
    $("history-page").textContent = `共 ${data.total} 个版本`;
    $("history-previous").disabled = state.historyOffset === 0;
    $("history-next").disabled = state.historyOffset + pageSize >= data.total;
    if (data.items[0]) showVersion(data.items[0]);
  } catch (error) {
    if (sequence === state.historyRequest) {
      $("history-list").replaceChildren(text("p", "读取失败，点击「历史版本」重试。", "panel-description"));
      report(error);
    }
  }
}

async function openHistory() {
  if (state.busy || !state.selected) return;
  state.tab = "history";
  state.historyOffset = 0;
  $("content-panel").hidden = true;
  $("history-panel").hidden = false;
  $("source-panel").hidden = true;
  $("content-tab").setAttribute("aria-selected", "false");
  $("history-tab").setAttribute("aria-selected", "true");
  $("source-tab").setAttribute("aria-selected", "false");
  await loadHistory();
}

async function openSource() {
  if (state.busy || state.selected?.source !== "markdown") return;
  const id = state.selected.id;
  const sequence = ++state.historyRequest;
  state.tab = "source";
  $("content-panel").hidden = true;
  $("history-panel").hidden = true;
  $("source-panel").hidden = false;
  for (const name of ["content", "history", "source"]) $(name + "-tab").setAttribute("aria-selected", String(name === "source"));
  $("source-info").textContent = "正在读取来源…";
  $("source-text").textContent = "";
  $("source-download").hidden = true;
  try {
    const source = await request(`${base}/${id}/source`);
    if (sequence !== state.historyRequest || state.selected?.id !== id) return;
    const candidate = source.candidate;
    $("source-info").textContent = `${source.file_name} · 第 ${candidate.start_line}–${candidate.end_line} 行。以下是导入时的原文，保留原始来源；你之后的修改在「题目内容」与「历史版本」中查看。`;
    $("source-text").textContent = candidate.raw_text;
    $("source-download").href = `/question-bank/imports/${source.import_id}/document`;
    $("source-download").hidden = false;
  } catch (error) {
    if (sequence === state.historyRequest) {
      $("source-info").textContent = "读取失败，点击「来源原文」重试。";
      report(error);
    }
  }
}

async function checkConnection() {
  try {
    await request("/health/ready");
    $("connection").className = "connection ok";
    $("connection").replaceChildren(document.createElement("i"), document.createTextNode("题库已连接"));
  } catch {
    $("connection").className = "connection error";
    $("connection").replaceChildren(document.createElement("i"), document.createTextNode("题库连接异常"));
  }
}

function actualCategory() {
  return state.category && state.category !== "uncategorized" ? state.category : "";
}

function categoryOptions(select, value) {
  select.replaceChildren();
  const none = text("option", "未分类");
  none.value = "";
  select.append(none);
  for (const category of state.categories) {
    const option = text("option", category.name);
    option.value = category.id;
    select.append(option);
  }
  // Refreshing classifications must not silently change an editor draft.
  if (value && !state.categories.some((category) => category.id === value)) {
    const missing = text("option", "原分类（请刷新分类列表）");
    missing.value = value;
    select.append(missing);
  }
  select.value = value;
}

async function loadCategories() {
  const sequence = ++state.categoryRequest;
  try {
    const data = await request("/question-bank/categories");
    if (sequence !== state.categoryRequest) return;
    state.categories = data.items;
    state.categoryCounts = data;
    categoryOptions($("question-category"), $("question-category").value);
    $("nav-count").textContent = data.total_questions;
    renderCategories();
    renderList();
    updateSaveState();
  } catch (error) {
    if (sequence === state.categoryRequest) {
      $("category-list").replaceChildren(text("p", "分类读取失败，请点击题目列表上方的「刷新」重试。", "category-help"));
    }
    throw error;
  }
}

function renderCategories() {
  if (!state.categoryCounts) return;
  const list = $("category-list");
  list.replaceChildren();
  const choices = [
    { id: "", name: "全部题目", question_count: state.categoryCounts.total_questions },
    { id: "uncategorized", name: "未分类", question_count: state.categoryCounts.uncategorized_count },
    ...state.categories,
  ];
  for (const category of choices) {
    const row = text("div", "", "category-row");
    const button = text("button", "", "category-button");
    button.type = "button";
    button.disabled = state.busy;
    button.setAttribute("aria-pressed", String(state.category === category.id));
    button.append(text("span", category.name), text("span", String(category.question_count), "category-count"));
    button.addEventListener("click", () => switchCategory(category.id).catch(report));
    row.append(button);
    if (category.id && category.id !== "uncategorized") {
      const rename = text("button", "编辑", "text-button category-rename");
      rename.type = "button";
      rename.disabled = state.busy;
      rename.setAttribute("aria-label", `重命名分类：${category.name}`);
      rename.addEventListener("click", () => openCategoryDialog(category));
      row.append(rename);
    }
    list.append(row);
  }
  $("category-title").textContent = choices.find((category) => category.id === state.category)?.name || "全部题目";
}

function clearEditor() {
  state.editing = false;
  state.selected = null;
  state.isNew = false;
  state.creation = null;
  ++state.historyRequest;
  $("editor").hidden = true;
  $("welcome").hidden = false;
  $("conflict-actions").hidden = true;
  updateSaveState();
}

async function switchCategory(id) {
  if (state.category === id || !canLeave()) return;
  state.category = id;
  state.offset = 0;
  state.checked.clear();
  clearEditor();
  renderCategories();
  await loadList();
}

function renderBatchControls() {
  $("selection-count").textContent = String(state.checked.size);
  $("move-questions").disabled = state.busy || state.listLoading || !state.checked.size;
  $("select-page").disabled = state.busy || state.listLoading || state.listError || !state.items.length;
  $("select-page").checked = state.items.length > 0 && state.items.every((item) => state.checked.has(item.id));
  $("select-page").indeterminate = state.checked.size > 0 && !$("select-page").checked;
}

function openCategoryDialog(category = null) {
  if (state.busy) return;
  state.categoryEditing = category;
  state.categoryCreation = null;
  $("category-dialog-title").textContent = category ? "重命名分类" : "新建分类";
  $("category-name").value = category?.name || "";
  $("category-dialog-message").hidden = true;
  $("category-dialog").showModal();
  $("category-name").focus();
}

function dialogMessage(id, error) {
  $(id).textContent = error instanceof ApiError ? error.message : "操作未完成，请重试。";
  $(id).hidden = false;
}

on("new-category", "click", () => openCategoryDialog());
on("close-category-dialog", "click", () => { if (!state.categoryBusy) $("category-dialog").close(); });
$("category-dialog").addEventListener("cancel", (event) => { if (state.categoryBusy) event.preventDefault(); });
on("category-form", "submit", async (event) => {
  event.preventDefault();
  if (state.categoryBusy) return;
  const name = $("category-name").value.trim();
  if (!name) { $("category-name").focus(); return; }
  const editing = state.categoryEditing;
  if (!editing && state.categoryCreation?.name !== name) state.categoryCreation = { name, id: crypto.randomUUID() };
  state.categoryBusy = true;
  for (const id of ["category-name", "save-category", "close-category-dialog"]) $(id).disabled = true;
  try {
    await request(`/question-bank/categories${editing ? `/${editing.id}` : ""}`, {
      method: editing ? "PATCH" : "POST",
      body: JSON.stringify(editing ? { name, expected_revision: editing.revision } : { name, request_id: state.categoryCreation.id }),
    });
    $("category-dialog").close();
    await loadCategories();
    notify(editing ? "分类名称已更新。" : "分类已创建，可以勾选题目移动到这里。");
  } catch (error) {
    dialogMessage("category-dialog-message", error);
    if (!$("category-dialog").open) report(error);
  } finally {
    state.categoryBusy = false;
    for (const id of ["category-name", "save-category", "close-category-dialog"]) $(id).disabled = false;
  }
});
on("select-page", "change", () => {
  state.checked = $("select-page").checked ? new Set(state.items.map((item) => item.id)) : new Set();
  renderList();
});
on("move-questions", "click", () => {
  if (!state.checked.size || !canLeave()) return;
  categoryOptions($("move-category"), actualCategory());
  $("move-description").textContent = `将选中的 ${state.checked.size} 道题目放入同一个分类，题目、答案和笔记保持原文。`;
  $("move-dialog-message").hidden = true;
  $("move-dialog").showModal();
  $("move-category").focus();
});
on("close-move-dialog", "click", () => { if (!state.moveBusy) $("move-dialog").close(); });
$("move-dialog").addEventListener("cancel", (event) => { if (state.moveBusy) event.preventDefault(); });
on("move-form", "submit", async (event) => {
  event.preventDefault();
  if (state.moveBusy || state.busy) return;
  const questions = state.items.filter((item) => state.checked.has(item.id)).map((item) => ({ id: item.id, expected_revision: item.revision }));
  if (!questions.length) return;
  state.moveBusy = true;
  for (const id of ["move-category", "confirm-move", "close-move-dialog"]) $(id).disabled = true;
  setBusy(true);
  try {
    await request(`${base}/classify`, { method: "POST", body: JSON.stringify({ category_id: $("move-category").value || null, questions }) });
    $("move-dialog").close();
    clearEditor();
    await loadCategories();
    await loadList();
    notify(`已移动 ${questions.length} 道题目。`);
  } catch (error) {
    dialogMessage("move-dialog-message", error);
    if (!$("move-dialog").open) report(error);
  } finally {
    state.moveBusy = false;
    for (const id of ["move-category", "confirm-move", "close-move-dialog"]) $(id).disabled = false;
    setBusy(false);
  }
});

on("edit-question", "click", () => {
  if (!state.selected || state.busy) return;
  state.editing = true;
  updateSaveState();
  inputs.question_text.focus();
});
on("cancel-question", "click", () => {
  if (state.busy || (isDirty() && !window.confirm("放弃本次未保存的题目修改？"))) return;
  if (state.selected) { populate(state.selected); $("edit-question").focus(); }
  else { clearEditor(); renderList(); $("new-question").focus(); }
});
on("new-question", "click", newQuestion);
on("question-form", "submit", saveQuestion);
on("delete-question", "click", deleteQuestion);
on("reload-current", "click", () => state.selected && selectQuestion(state.selected.id));
on("dismiss-message", "click", () => { $("message").hidden = true; });
on("refresh", "click", () => Promise.all([loadList(), loadCategories(), checkConnection()]));
on("content-tab", "click", showContent);
on("history-tab", "click", openHistory);
on("source-tab", "click", openSource);
on("search-form", "submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  state.search = $("search").value;
  state.offset = 0;
  await loadList();
});
on("active-filter", "change", async () => {
  state.filter = $("active-filter").value;
  state.offset = 0;
  await loadList();
});
on("previous-page", "click", async () => { state.offset = Math.max(0, state.offset - pageSize); await loadList(); });
on("next-page", "click", async () => { state.offset += pageSize; await loadList(); });
on("history-previous", "click", async () => { state.historyOffset = Math.max(0, state.historyOffset - pageSize); await loadHistory(); });
on("history-next", "click", async () => { state.historyOffset += pageSize; await loadHistory(); });
on("question-form", "input", updateSaveState);
on("question-form", "keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    if (!state.busy) $("question-form").requestSubmit();
  }
});
window.addEventListener("beforeunload", (event) => {
  if (isDirty() || state.busy) { event.preventDefault(); event.returnValue = ""; }
});

setupMarkdownImport({
  request,
  canOpen: canLeave,
  onImported: async (result) => {
    state.offset = 0;
    state.search = "";
    state.filter = "";
    state.category = "uncategorized";
    clearEditor();
    await loadCategories();
    $("search").value = "";
    $("active-filter").value = "";
    await loadList();
    await checkConnection();
    notify(`已导入 ${result.imported_count} 道题目，可在左侧选择查看。`);
  },
});

await Promise.all([loadList(), loadCategories().catch(report), checkConnection()]);
