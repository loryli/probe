import { renderRecord } from "./record_view.js?v=20261110-7";

const $ = (id) => document.getElementById(id);
const base = "/interview-plans";
const pageSize = 10;
const planContractVersion = "planner.v2";
const steps = [
  { id: 1, label: "训练依据" },
  { id: 2, label: "题库范围" },
  { id: 3, label: "方向与节奏" },
  { id: 4, label: "预览与确认" },
];

const state = {
  options: null, plans: [], planTotal: 0, planOffset: 0,
  plan: null, editing: false, step: 1, maxStep: 1,
  selectedProjects: null, selectedQuestions: null,
  pendingId: null, busy: false, dirty: false, progressTimer: null,
};

const statusLabels = { running: "生成中", ready: "待确认", confirmed: "已确认", failed: "失败" };
const statusTones = { running: "info", ready: "warning", confirmed: "success", failed: "danger" };
const priorityLabels = { high: "高优先级", medium: "中优先级", low: "低优先级" };
const formatDate = (value) => new Date(value).toLocaleString("zh-CN", { hour12: false });
const formatSeconds = (value) => {
  const seconds = Number(value) || 0;
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
};

function randomSample(items, size) {
  const pool = [...items];
  for (let index = pool.length - 1; index > 0; index -= 1) {
    const other = Math.floor(Math.random() * (index + 1));
    [pool[index], pool[other]] = [pool[other], pool[index]];
  }
  return new Set(pool.slice(0, size));
}

const sameList = (left, right) =>
  left.length === right.length && left.every((value, index) => value === right[index]);

function currentConfig() {
  return {
    mode: $("mode").value,
    resume_id: $("resume-select").value,
    source_version: Number($("version-select").value),
    direction: $("direction").value.trim(),
    duration_minutes: Number($("duration").value),
    project_ratio: Number($("ratio").value) / 100,
    max_total_followups: Number($("followups").value),
    project_ids: [...(state.selectedProjects || [])].sort(),
    question_ids: [...(state.selectedQuestions || [])].sort(),
  };
}

// 配置改动后，旧结果不能被当作新配置确认；重新生成会形成新记录。
function configDrift() {
  const saved = state.plan?.inputs;
  if (!saved) return false;
  const current = currentConfig();
  return current.mode !== saved.mode
    || current.resume_id !== saved.resume_id
    || current.source_version !== saved.source_version
    || current.direction !== (saved.direction || "")
    || current.duration_minutes !== saved.duration_minutes
    || Math.abs(current.project_ratio - saved.project_ratio) > 0.0001
    || current.max_total_followups !== saved.max_total_followups
    || !sameList(current.project_ids, [...saved.project_ids].sort())
    || !sameList(current.question_ids, [...saved.question_ids].sort());
}

function markDirty() {
  state.dirty = true;
  refreshConfirmState();
}

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
    throw new ApiError("连接失败或请求超时，输入已保留。可点击「读取本次结果」确认状态。", 0);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const fallback = {
      404: "资料或计划已不存在，请刷新列表。",
      409: "资料或计划已变化，请保留输入并重新生成。",
      422: "配置不符合要求，请检查简历版本、资料选择与输入范围。",
      503: "服务暂不可用，请确认数据库与模型配置。",
    };
    throw new ApiError(
      typeof detail?.message === "string" ? detail.message :
        fallback[response.status] || "操作未完成，输入已保留，请稍后重试。",
      response.status, detail?.code,
    );
  }
  return response.status === 204 ? null : response.json();
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function notify(message, error = false) {
  $("message-text").textContent = message;
  $("message").classList.toggle("error", error);
  $("message").hidden = false;
}

function report(error) {
  notify(error instanceof ApiError ? error.message : "操作未完成，输入已保留，请重试。", true);
}

function setBusy(value) {
  state.busy = value;
  // 生成期间锁定会改变请求含义的操作；读取本次结果入口保持可用。
  for (const node of $("plan-form").querySelectorAll("input, select, button")) node.disabled = value;
  for (const node of document.querySelectorAll("#wizard-nav button, #back-to-plans, #edit-plan, #cancel-plan, #adjust-config")) node.disabled = value;
  $("new-plan").disabled = value;
  $("read-pending").disabled = value;
  $("generate-plan").disabled = value || Boolean(state.pendingId);
  $("wizard-next").disabled = value;
  $("wizard-previous").disabled = value;
  $("confirm-plan").disabled = value || !canConfirm();
  if (!value) {
    $("read-pending").disabled = false;
    $("wizard-next").disabled = false;
    $("wizard-previous").disabled = false;
    // 还原后按当前状态重新计算按钮可用性（例如生成中的计划不能调整）。
    updatePlanMode();
  }
}

function startProgress() {
  const started = Date.now();
  const line = $("generate-progress");
  line.hidden = false;
  line.textContent = "已提交规划请求，模型模式通常需要数十秒；输入已保留，不会自动重试。";
  $("plan-status").textContent = "生成中";
  state.progressTimer = setInterval(() => {
    const seconds = Math.round((Date.now() - started) / 1000);
    line.textContent = `正在生成计划… 已等待 ${seconds} 秒。输入已保留，不会自动重试收费请求。`;
  }, 1000);
}

function stopProgress() {
  if (state.progressTimer) clearInterval(state.progressTimer);
  state.progressTimer = null;
  $("generate-progress").hidden = true;
  if (state.plan) setStatusBadge(state.plan.status);
}

function setBadge(text, tone = "") {
  const badge = $("plan-status");
  badge.textContent = text;
  badge.className = `ds-badge${tone ? ` ds-badge--${tone}` : ""}`;
}

function setStatusBadge(status) {
  setBadge(status ? (statusLabels[status] || status) : "未生成", status ? statusTones[status] : "");
}

/* ---------- 视图切换：目录 / 详情 / 创建 ---------- */

function showDirectory() {
  $("plan-directory").hidden = false;
  $("planner-workspace").hidden = true;
  document.body.classList.remove("plan-detail-open");
}

function showWorkspace() {
  $("plan-directory").hidden = true;
  $("planner-workspace").hidden = false;
  document.body.classList.add("plan-detail-open");
}

/* ---------- 选项与选择器 ---------- */

function updateCounts() {
  $("project-selected-count").textContent = String(state.selectedProjects?.size ?? 0);
  $("question-selected-count").textContent = String(state.selectedQuestions?.size ?? 0);
  const selectedProjects = state.selectedProjects?.size ?? 0;
  const selectedQuestions = state.selectedQuestions?.size ?? 0;
  $("selection-summary").textContent = `本次依据：简历 v${$("version-select").value || "—"} · ${selectedProjects} 个项目 · ${selectedQuestions} 道题库题。`;
}

function renderResumes() {
  const options = state.options;
  const select = $("resume-select");
  select.replaceChildren();
  if (!options.resumes.length) {
    const item = element("option", "暂无简历，请先在「简历与项目」创建");
    item.value = "";
    select.append(item);
    $("no-resume-hint").hidden = false;
  } else {
    $("no-resume-hint").hidden = true;
    for (const resume of options.resumes) {
      const item = element("option", `${resume.name} · 最新 v${resume.content_version}${resume.is_active ? "" : "（已停用）"}`);
      item.value = resume.id;
      select.append(item);
    }
    if (options.selected_resume_id) select.value = options.selected_resume_id;
  }
}

function renderVersions() {
  const options = state.options;
  const select = $("version-select");
  select.replaceChildren();
  if (!options.versions.length) {
    const item = element("option", "暂无版本");
    item.value = "";
    select.append(item);
  }
  for (const version of options.versions) {
    const item = element("option", `v${version.version} · ${version.name}`);
    item.value = String(version.version);
    select.append(item);
  }
  if (options.selected_version) select.value = String(options.selected_version);
  $("version-hint").textContent = options.selected_version
    ? `计划固定引用简历 v${options.selected_version}；下方只列出该版本中已核对的项目。`
    : "所选简历暂无版本，请先在「简历与项目」保存简历文字。";
}

function renderModelStatus() {
  const status = state.options.model;
  $("model-status").textContent = status.configured
    ? `已配置 ${status.model}。模型模式调用 Qwen；规则模式完全本地生成。`
    : `Qwen 未配置（缺少 ${(status.missing || []).join("、")}）。可改用规则模式生成，不调用模型。`;
}

function renderPickers() {
  const options = state.options;
  const projectBox = $("project-options");
  const questionBox = $("question-options");
  projectBox.replaceChildren();
  questionBox.replaceChildren();
  const maxProjects = options.limits.max_projects;
  const maxQuestions = options.limits.max_questions;

  if (!options.projects.length) {
    projectBox.append(element("p", "所选简历版本下没有已核对项目。可在「简历与项目」勾选「我已核对」并保存，或只选择题库题。", "picker-empty"));
  }
  for (const project of options.projects) {
    const label = element("label", "", "picker-item");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = state.selectedProjects.has(project.id);
    input.addEventListener("change", () => {
      if (input.checked) {
        if (state.selectedProjects.size >= maxProjects) {
          input.checked = false;
          notify(`最多选择 ${maxProjects} 个项目`, true);
          return;
        }
        state.selectedProjects.add(project.id);
      } else {
        state.selectedProjects.delete(project.id);
      }
      updateCounts();
      markDirty();
    });
    const text = element("span", project.name, "picker-text");
    text.append(element("small", `简历 v${project.source_version} · ${project.fact_count} 条事实 · ${project.tech_stack.join("、") || "未填技术栈"}`));
    label.append(input, text);
    projectBox.append(label);
  }

  if (!options.questions.length) {
    questionBox.append(element("p", "没有启用题目。请在「个人题库」启用题目后再生成计划，或只选有事实的项目。", "picker-empty"));
  }
  for (const question of options.questions) {
    const label = element("label", "", "picker-item");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = state.selectedQuestions.has(question.id);
    input.addEventListener("change", () => {
      if (input.checked) {
        if (state.selectedQuestions.size >= maxQuestions) {
          input.checked = false;
          notify(`一次计划最多选用 ${maxQuestions} 道题目`, true);
          return;
        }
        state.selectedQuestions.add(question.id);
      } else {
        state.selectedQuestions.delete(question.id);
      }
      updateCounts();
      markDirty();
    });
    const text = element("span", question.question_text || "（题干为空）", "picker-text");
    text.append(element("small", `内容版本 v${question.content_version}`));
    label.append(input, text);
    questionBox.append(label);
  }
  $("question-hint").textContent = `当前共 ${options.question_total} 道启用题目，按导入顺序列出；默认随机选用 ${maxQuestions} 道，可重新抽取或逐题调整。`;
  updateCounts();
}

function syncPickers(settings) {
  const options = state.options;
  const projectIds = new Set(options.projects.map((item) => item.id));
  const questionIds = new Set(options.questions.map((item) => item.id));
  if (settings.selection) {
    state.selectedProjects = new Set(settings.selection.project_ids.filter((id) => projectIds.has(id)));
    state.selectedQuestions = new Set(settings.selection.question_ids.filter((id) => questionIds.has(id)));
  } else {
    if (settings.reset || state.selectedProjects === null) {
      state.selectedProjects = new Set(projectIds);
    } else {
      state.selectedProjects = new Set([...state.selectedProjects].filter((id) => projectIds.has(id)));
    }
    if (settings.resetQuestions || state.selectedQuestions === null) {
      state.selectedQuestions = randomSample([...questionIds], options.limits.max_questions);
    } else {
      state.selectedQuestions = new Set([...state.selectedQuestions].filter((id) => questionIds.has(id)));
    }
  }
  renderPickers();
}

async function loadOptions(resumeId = null, version = null, settings = {}) {
  const params = new URLSearchParams();
  if (resumeId) params.set("resume_id", resumeId);
  if (version) params.set("source_version", String(version));
  const query = params.toString();
  state.options = await request(`${base}/options${query ? `?${query}` : ""}`);
  renderResumes();
  renderVersions();
  renderModelStatus();
  syncPickers(settings);
}

/* ---------- 目录 ---------- */

function renderPlanList() {
  const list = $("plan-list");
  list.replaceChildren();
  $("plan-count").textContent = String(state.planTotal);
  $("plan-page").textContent = `第 ${Math.floor(state.planOffset / pageSize) + 1} 页`;
  $("plan-previous").disabled = state.planOffset <= 0;
  $("plan-next").disabled = state.planOffset + pageSize >= state.planTotal;
  $("planner-welcome").hidden = state.planTotal > 0;
  if (!state.plans.length) {
    // 从未创建计划时由空状态说明，不重复铺一条占位文案。
    if (state.planTotal) list.append(element("p", "本页没有记录，可翻回上一页。", "list-placeholder"));
    return;
  }
  for (const item of state.plans) {
    const button = element("button", "", `plan-card${state.plan?.id === item.id ? " selected" : ""}`);
    button.type = "button";
    button.setAttribute("aria-pressed", String(state.plan?.id === item.id));
    button.append(element("span", statusLabels[item.status] || item.status, `plan-badge ${item.status}`));
    const body = element("span", "", "plan-card-body");
    body.append(element("strong", item.direction || "未填写考察方向"));
    body.append(element("small", `简历 v${item.source_version} · 预计 ${item.duration_minutes} 分钟 · ${item.objective_count} 个目标 · ${item.provider === "rule" ? "规则模式" : "模型模式"}`));
    body.append(element("small", `生成于 ${formatDate(item.created_at)}`));
    button.append(body);
    button.addEventListener("click", () => selectPlan(item.id));
    list.append(button);
  }
}

async function loadPlans() {
  const data = await request(`${base}?limit=${pageSize}&offset=${state.planOffset}`);
  state.plans = data.items;
  state.planTotal = data.total;
  renderPlanList();
}

/* ---------- 分步表单 ---------- */

function fillForm(inputs) {
  $("direction").value = inputs.direction || "";
  $("duration").value = String(inputs.duration_minutes);
  $("ratio").value = String(Math.round(inputs.project_ratio * 100));
  $("ratio-value").textContent = `${$("ratio").value}%`;
  $("followups").value = String(inputs.max_total_followups);
  $("mode").value = inputs.mode;
}

function stepLabel(step) {
  return steps.find((item) => item.id === step)?.label || "";
}

function validateStep(step) {
  if (step === 1) {
    if (!state.options?.resumes.length) return { message: "当前没有可用的简历，请先在「简历与项目」保存一份简历。", focus: "resume-select" };
    if (!$("resume-select").value) return { message: "请选择一份简历。", focus: "resume-select" };
    if (!state.options?.versions.length) return { message: "所选简历还没有版本，请先保存简历文字。", focus: "version-select" };
    if (!$("version-select").value) return { message: "请选择要固定引用的简历版本。", focus: "version-select" };
    return null;
  }
  if (step === 2) {
    if (!state.selectedProjects?.size && !state.selectedQuestions?.size) {
      return { message: "请至少选择一个已核对项目或一道启用题目；题源至少要有一种可用材料。", focus: null };
    }
    return null;
  }
  if (step === 3) {
    const duration = Number($("duration").value);
    if (!Number.isFinite(duration) || duration < 10 || duration > 180) return { message: "预计时长需在 10–180 分钟之间。", focus: "duration" };
    const followups = Number($("followups").value);
    if (!Number.isFinite(followups) || followups < 0 || followups > 30) return { message: "追问上限需在 0–30 之间。", focus: "followups" };
  }
  if (step === 4) {
    if (!state.selectedProjects?.size && !state.selectedQuestions?.size) {
      return { message: "请至少选择一个已核对项目或一道启用题目。", focus: null, goto: 1 };
    }
  }
  return null;
}

function goStep(target, { focus = true } = {}) {
  const step = Math.min(4, Math.max(1, target));
  state.step = step;
  state.maxStep = Math.max(state.maxStep, step);
  updateWizard();
  if (step === 4) renderReviewSummary();
  if (focus) $(`step-title-${step}`)?.focus({ preventScroll: false });
}

function advance() {
  for (let step = state.step; step < 4; step += 1) {
    const problem = validateStep(step);
    if (problem) {
      goStep(problem.goto || step);
      notify(problem.message, true);
      if (problem.focus) $(problem.focus)?.focus();
      return;
    }
  }
  goStep(state.step + 1);
}

function jumpTo(target) {
  if (target > state.step) {
    for (let step = state.step; step < target; step += 1) {
      const problem = validateStep(step);
      if (problem) {
        notify(problem.message, true);
        if (problem.focus) $(problem.focus)?.focus();
        return;
      }
    }
  }
  goStep(target);
}

function renderReviewSummary() {
  const config = currentConfig();
  const projects = (state.options?.projects || []).filter((item) => state.selectedProjects?.has(item.id));
  const rows = [
    ["考察方向", config.direction || "未填写（按资料默认方向）"],
    ["简历版本", `v${config.source_version || "—"}`],
    ["项目依据", projects.length ? projects.map((item) => item.name).join("、") : "不选项目，只用题库题"],
    ["题库范围", `${config.question_ids.length} 道候选题目`],
    ["预计时长", `${config.duration_minutes || "—"} 分钟 · 项目题约占 ${Math.round(config.project_ratio * 100)}%`],
    ["追问边界", config.max_total_followups ? `总上限 ${config.max_total_followups} 次` : "自动（按计划约束）"],
    ["生成方式", config.mode === "rule" ? "规则模式 · 不调用模型" : "Qwen 模型规划 · 一次调用"],
  ];
  const list = $("review-summary");
  list.replaceChildren();
  for (const [label, value] of rows) {
    const term = element("dt", label);
    const detail = element("dd", value);
    list.append(term, detail);
  }
  $("review-note").textContent = state.plan
    ? "生成会创建一条新计划记录，不覆盖正在查看的计划。结果就绪后再确认。"
    : "生成完成后在这里预览目标、预算与引用；确认之前计划不会用于面试。";
}

function updateWizard() {
  for (const section of document.querySelectorAll("[data-wizard-section]")) {
    section.hidden = Number(section.dataset.wizardSection) !== state.step;
  }
  for (const button of document.querySelectorAll("[data-plan-step]")) {
    const target = Number(button.dataset.planStep);
    button.setAttribute("aria-current", String(target === state.step));
    button.classList.toggle("visited", target <= state.maxStep);
  }
  $("wizard-position").textContent = `第 ${state.step} / 4 步 · ${stepLabel(state.step)}`;
  $("wizard-previous").hidden = state.step === 1;
  $("wizard-next").hidden = state.step === 4;
  $("generate-plan").hidden = state.step !== 4;
  $("plan-meta").textContent = state.step === 4
    ? "点击「生成计划」才会提交并保存新记录。"
    : "前 3 步只保留本页配置；目标时长到时不强制结束。";
  updateCounts();
}

/* ---------- 结果 ---------- */

function renderResultHeader(plan) {
  $("plan-title").textContent = plan.status === "confirmed" ? "已确认计划" : plan.status === "ready" ? "待确认计划" : "计划详情";
  setStatusBadge(plan.status);
  $("result-state").textContent = `简历 v${plan.source_version} · ${plan.provider === "rule" ? "规则模式" : `模型 ${plan.model || "Qwen"}`}`;
  $("plan-meta").textContent = plan.confirmed_at
    ? `已于 ${formatDate(plan.confirmed_at)} 确认保存，资料引用已固定。`
    : "目标时长仅作节奏参考，到时不强制结束。";
  $("delete-plan").hidden = false;
  $("plan-conflict").hidden = true;
}

function renderPlanSubtitle(plan) {
  const subtitle = plan
    ? `简历 v${plan.source_version} · ${plan.inputs.project_ids.length} 个项目 · ${plan.inputs.question_ids.length} 道题库题 · ${plan.inputs.duration_minutes} 分钟`
    : state.options
      ? `简历 ${state.options.resumes.length} 份 · 已核对项目 ${state.options.projects.length} 个 · 启用题目 ${state.options.question_total} 道`
      : "";
  $("plan-subtitle").textContent = subtitle;
}

function clearResultBody() {
  $("result-summary").replaceChildren();
  $("result-meta").replaceChildren();
  $("result-budgets").replaceChildren();
  $("result-objectives").replaceChildren();
  $("notes-system").replaceChildren();
  $("notes-model").replaceChildren();
  $("notes-policy").replaceChildren();
}

function appendMeta(container, texts) {
  texts.forEach((text, index) => {
    if (index > 0) container.append(document.createTextNode(" · "));
    container.append(element("span", text));
  });
}

function fillList(id, items) {
  for (const text of items) $(id).append(element("li", text));
}

function renderNotes(result) {
  const notes = result.notes || {};
  const system = notes.system || [];
  const model = notes.model || [];
  const policy = notes.policy || [];
  const legacy = !system.length && !model.length && !policy.length
    ? (result.limitations || []) : [];
  const modelItems = model.length ? [...model] : [...legacy];
  if (legacy.length) {
    modelItems.unshift("此计划为优化前生成，提示未区分来源，以下均按模型自评处理。");
  }
  fillList("notes-system", system.length ? system : ["无。"]);
  fillList("notes-model", modelItems.length ? modelItems : ["无。"]);
  fillList("notes-policy", policy.length ? policy : ["无。"]);
}

function buildReferenceDetails(objective, plan) {
  const factIndex = new Map();
  for (const project of plan.snapshot?.projects || []) {
    for (const fact of project.facts) factIndex.set(fact.id, { project: project.name, fact });
  }
  const questionIndex = new Map(
    (plan.snapshot?.questions || []).map((item) => [item.id, item]),
  );
  const details = element("details", "", "objective-references");
  details.append(element("summary",
    `核对引用明细（${objective.project_fact_ids.length} 条事实 / ${objective.question_ids.length} 道题）`));
  const box = element("div");
  let count = 0;
  for (const id of objective.project_fact_ids) {
    const item = factIndex.get(id);
    const line = element("p", "", "reference-item");
    if (item) {
      line.append(element("strong", `［事实·${item.fact.fact_type}］`),
        document.createTextNode(`${item.project}：${item.fact.content}`));
      count += 1;
    } else {
      line.append(document.createTextNode(`事实 ${id} 不在当前快照中，请重新生成计划。`));
    }
    box.append(line);
  }
  for (const id of objective.question_ids) {
    const item = questionIndex.get(id);
    const line = element("p", "", "reference-item");
    if (item) {
      line.append(element("strong", "［启用题目］"), document.createTextNode(item.question_text));
      count += 1;
    } else {
      line.append(document.createTextNode(`题目 ${id} 不在当前快照中，请重新生成计划。`));
    }
    box.append(line);
  }
  if (!count) box.append(element("p", "此目标没有可展示的引用原文。", "reference-item"));
  details.append(box);
  return details;
}

function renderPlan(plan) {
  $("plan-result").hidden = false;
  renderResultHeader(plan);
  renderPlanSubtitle(plan);
  clearResultBody();
  const summary = $("result-summary");
  const metaTexts = [
    `资料：简历 v${plan.source_version}`,
    `${plan.snapshot?.projects.length ?? 0} 个项目 / ${plan.snapshot?.questions.length ?? 0} 道题`,
    `耗时 ${plan.elapsed_ms ?? 0} ms`,
  ];
  if (plan.input_tokens !== null && plan.input_tokens !== undefined) {
    metaTexts.push(`token ${plan.input_tokens} / ${plan.output_tokens ?? 0}`);
  }
  if (plan.prompt_version) metaTexts.push(`prompt ${plan.prompt_version}`);
  if (plan.schema_version) metaTexts.push(`契约 ${plan.schema_version}`);
  appendMeta($("result-meta"), metaTexts);
  refreshConfirmState(plan);

  if (plan.status === "failed" || plan.status === "running") {
    const message = plan.status === "running"
      ? "计划仍在生成中，可稍后点击「读取本次结果」。"
      : `${plan.error_message || "本次规划未成功"}${plan.error_code ? `（${plan.error_code}）` : ""}`;
    summary.append(element("div", message, "result-error"));
    if (plan.status === "failed") {
      summary.append(element("p", "输入已保留：可检查模型配置，改用「规则模式」，或调整资料后重新生成。不会自动重试模型调用。", "material-help"));
    }
    renderNotes({ limitations: [] });
    $("result-note").textContent = plan.status === "running"
      ? "生成结束后再核对目标与引用。"
      : "失败不会覆盖已保存的计划；调整后可再次生成。";
    return;
  }
  const result = plan.result;
  if (!result) {
    summary.append(element("div", "此计划没有可展示的结果，请重新生成。", "result-error"));
    renderNotes({ limitations: [] });
    return;
  }
  summary.textContent = result.session_summary || "依据所选资料的面试计划";
  if (plan.schema_version && plan.schema_version !== planContractVersion) {
    summary.append(element(
      "div",
      "此计划由旧版规则生成，缺少模拟面试所需的运行依据，不能开始面试或再次确认；请按当前输入重新生成。",
      "result-error",
    ));
  }
  const budgets = [
    ["目标时长", formatSeconds(result.duration_target_sec), "仅作节奏参考"],
    ["开场 / 收尾", `${result.opening_buffer_sec}s / ${result.closing_buffer_sec}s`, "缓冲"],
    ["项目题预算", formatSeconds(result.source_time_budget_sec.project), "按时间近似控制"],
    ["题库题预算", formatSeconds(result.source_time_budget_sec.question_bank), "按时间近似控制"],
    ["追问总上限", String(result.constraints.max_total_followups), `每目标最多 ${result.constraints.max_followups_per_objective}`],
  ];
  if (result.coverage) {
    budgets.push([
      "资料覆盖",
      `${result.coverage.questions_used}/${result.coverage.questions_total} 道题`,
      `事实 ${result.coverage.facts_used}/${result.coverage.facts_total} 条`,
    ]);
  }
  if (result.candidate_scope) {
    budgets.push([
      "候选范围",
      `${result.candidate_scope.question_ids.length} 道题`,
      `事实 ${result.candidate_scope.project_fact_ids.length} 条 · 可动态选用`,
    ]);
  }
  for (const [title, value, note] of budgets) {
    const card = element("div", "", "budget-card");
    card.append(element("small", title), element("strong", value), element("small", note));
    $("result-budgets").append(card);
  }
  for (const [index, objective] of result.objectives.entries()) {
    const card = element("article", "", "objective-card");
    const heading = element("h5", "", "objective-heading");
    heading.append(element("span", String(index + 1).padStart(2, "0"), "objective-index"));
    heading.append(element("span", objective.topic, "objective-topic"));
    const tags = element("span", "", "objective-tags");
    tags.append(
      element("span", objective.source_preference === "project" ? "项目题" : "题库题", `objective-badge ${objective.source_preference === "project" ? "project" : "bank"}`),
      element("span", priorityLabels[objective.priority] || objective.priority, "objective-badge"),
      element("span", `约 ${formatSeconds(objective.time_budget_sec)}`, "objective-badge"),
      element("span", `追问 ≤ ${objective.max_followups}`, "objective-badge"),
    );
    heading.append(tags);
    card.append(heading);
    if (objective.observable_evidence.length) {
      card.append(element("p", `可观察证据：${objective.observable_evidence.join("；")}`, "objective-evidence"));
    }
    if (objective.completion_rule) card.append(element("p", `完成条件：${objective.completion_rule}`, "objective-completion"));
    card.append(element("p", `引用 ${objective.project_fact_ids.length} 条事实 · ${objective.question_ids.length} 道题目（均为生成时的固定版本）`, "objective-refs"));
    card.append(buildReferenceDetails(objective, plan));
    const context = (result.runtime_context || {})[objective.id];
    if (context) {
      const runtime = element("details", "", "objective-runtime");
      runtime.append(element("summary", "运行时上下文（后续面试直接读取）"));
      runtime.append(element("pre", context));
      card.append(runtime);
    }
    $("result-objectives").append(card);
  }
  if (!result.objectives.length) {
    $("result-objectives").append(element("p", "本次没有生成考察目标。", "picker-empty"));
  }
  renderNotes(result);
  $("result-note").textContent = plan.status === "confirmed"
    ? "计划已确认保存。重新生成会创建新计划，不覆盖此记录。"
    : "确认前请展开引用明细核对；模型提示未经核验，确认后计划固定。";
}

function canConfirm() {
  const plan = state.plan;
  if (!plan || plan.status !== "ready" || state.editing || state.busy) return false;
  if (plan.schema_version !== planContractVersion) return false;
  return !configDrift();
}

function refreshConfirmState(plan = state.plan) {
  const ready = Boolean(plan) && plan.status === "ready";
  const outdated = ready && plan.schema_version !== planContractVersion;
  const drift = ready && !outdated && !state.editing && configDrift();
  const changed = ready && !outdated && state.editing;
  $("confirm-plan").hidden = !ready || state.editing;
  $("confirm-plan").disabled = !canConfirm();
  $("adjust-config").hidden = !plan || state.editing;
  $("start-plan-interview").hidden = !(plan && plan.status === "confirmed" && plan.schema_version === planContractVersion);
  const hint = $("config-drift");
  hint.hidden = !outdated && !drift && !changed;
  if (outdated) {
    hint.textContent = "此计划由旧版规则生成，缺少运行时资料依据，无法确认；请按当前输入重新生成。";
  } else if (changed) {
    hint.textContent = "配置正在调整。当前结果保持只读，重新生成后才会形成新记录；确认按钮在调整期间不可用。";
  } else if (drift) {
    hint.textContent = "当前配置与这份结果不一致（或资料已变化）。请重新生成后再确认；旧结果仍可回看。";
  }
  $("start-plan-interview").title = "进入模拟面试页后选择这份已确认计划；本页不会自动创建场次。";
}

/* ---------- 计划操作 ---------- */

async function selectPlan(id) {
  if (state.busy || !canLeavePlan()) return;
  setBusy(true);
  try {
    const plan = await request(`${base}/${id}`);
    state.plan = plan;
    if (plan.status === "running") state.pendingId = plan.id;
    else if (state.pendingId === plan.id) state.pendingId = null;
    state.editing = false;
    state.step = 4;
    state.maxStep = 4;
    showWorkspace();
    await loadOptions(plan.resume_id, plan.source_version, { selection: plan.inputs });
    fillForm(plan.inputs);
    renderPlan(plan);
    updateWizard();
    renderPlanList();
    state.dirty = false;
    $("read-pending").hidden = !state.pendingId;
    updatePlanMode();
  } catch (error) {
    report(error);
  } finally {
    setBusy(false);
  }
}

async function newPlan() {
  if (state.busy) return;
  if (state.pendingId && !window.confirm("上一份计划的结果尚未确定。放弃读取并新建计划？可在计划记录里重新打开该计划读取结果。")) return;
  state.pendingId = null;
  if (!canLeavePlan()) return;
  state.editing = true;
  state.plan = null;
  state.step = 1;
  state.maxStep = 1;
  state.dirty = false;
  showWorkspace();
  updatePlanMode();
  updateWizard();
  $("plan-result").hidden = true;
  $("plan-title").textContent = "新建计划";
  renderPlanSubtitle(null);
  setStatusBadge(null);
  $("delete-plan").hidden = true;
  $("read-pending").hidden = !state.pendingId;
  $("plan-conflict").hidden = true;
  $("confirm-plan").hidden = true;
  $("adjust-config").hidden = true;
  $("start-plan-interview").hidden = true;
  $("config-drift").hidden = true;
  $("direction").value = "";
  $("duration").value = "45";
  $("ratio").value = "50";
  $("ratio-value").textContent = "50%";
  $("followups").value = "0";
  setBusy(true);
  try {
    await loadOptions(null, null, { reset: true, resetQuestions: true });
    renderPlanList();
    updateWizard();
    state.dirty = false;
    notify("已载入可选资料。先固定简历版本，再选题库范围。");
  } catch (error) {
    report(error);
  } finally {
    setBusy(false);
    goStep(1);
  }
}

async function generate(event) {
  event.preventDefault();
  if (state.busy || !state.editing) return;
  // 前 3 步按回车只前进，不触发收费生成；只有第 4 步才提交。
  if (state.step !== 4) { advance(); return; }
  const problem = validateStep(1) || validateStep(2) || validateStep(3) || validateStep(4);
  if (problem) {
    goStep(problem.goto || 1);
    notify(problem.message, true);
    if (problem.focus) $(problem.focus)?.focus();
    return;
  }
  const resumeId = $("resume-select").value;
  const version = Number($("version-select").value);
  // 随机只决定「选哪些」，提交仍按页面列表顺序，不改变题目先后。
  const questionOrder = new Map(
    (state.options?.questions || []).map((item, index) => [item.id, index]),
  );
  const selectedQuestions = [...(state.selectedQuestions || [])].sort(
    (left, right) => (questionOrder.get(left) ?? Number.MAX_SAFE_INTEGER)
      - (questionOrder.get(right) ?? Number.MAX_SAFE_INTEGER),
  );
  const payload = {
    request_id: crypto.randomUUID(),
    mode: $("mode").value,
    resume_id: resumeId,
    source_version: version,
    project_ids: [...(state.selectedProjects || [])],
    question_ids: selectedQuestions,
    direction: $("direction").value.trim(),
    project_ratio: Number($("ratio").value) / 100,
    duration_minutes: Number($("duration").value),
    max_total_followups: Number($("followups").value),
  };
  if (!payload.project_ids.length && !payload.question_ids.length) {
    goStep(1);
    notify("请至少选择一个已核对项目或一道启用题目", true);
    return;
  }
  setBusy(true);
  startProgress();
  state.pendingId = payload.request_id;
  $("read-pending").hidden = true;
  try {
    const plan = await request(base, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      timeoutMs: 330000,
    });
    state.plan = plan;
    state.editing = plan.status === "failed";
    state.pendingId = plan.status === "running" ? plan.id : null;
    state.dirty = false;
    state.step = 4;
    state.maxStep = 4;
    renderPlan(plan);
    updatePlanMode();
    updateWizard();
    notify(
      plan.status === "failed" ? "计划生成失败，请查看下方原因；输入已保留。" : "计划已生成，请核对目标后确认保存。",
      plan.status === "failed",
    );
    await loadPlans();
  } catch (error) {
    if (!(error instanceof ApiError) || error.status === 0) {
      $("read-pending").hidden = false;
    } else {
      state.pendingId = null;
    }
    report(error);
  } finally {
    stopProgress();
    setBusy(false);
  }
}

async function readPending() {
  if (!state.pendingId || state.busy) return;
  const planId = state.pendingId;
  setBusy(true);
  try {
    const plan = await request(`${base}/${planId}`);
    state.plan = plan;
    state.editing = plan.status === "failed";
    // 仍在生成时保留待查询标识，允许稍后继续读取同一任务。
    if (plan.status !== "running") state.pendingId = null;
    $("read-pending").hidden = !state.pendingId;
    if (plan.status !== "running") state.dirty = false;
    renderPlan(plan);
    updatePlanMode();
    await loadPlans();
  } catch (error) {
    report(error);
  } finally {
    setBusy(false);
  }
}

async function confirmPlan() {
  if (!state.plan || !canConfirm()) return;
  setBusy(true);
  try {
    const plan = await request(`${base}/${state.plan.id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: state.plan.revision }),
    });
    state.plan = plan;
    state.dirty = false;
    renderPlan(plan);
    updatePlanMode();
    await loadPlans();
    notify("计划已确认保存，资料引用已固定。");
    $("start-plan-interview").focus();
  } catch (error) {
    if (error.code === "plan_reference_stale" || error.code === "revision_conflict") {
      $("plan-conflict").hidden = false;
    }
    report(error);
  } finally {
    setBusy(false);
  }
}

async function deletePlan() {
  if (!state.plan || state.busy) return;
  if (!window.confirm("删除这份计划？普通接口与页面将不再返回它。")) return;
  setBusy(true);
  try {
    await request(`${base}/${state.plan.id}?expected_revision=${state.plan.revision}`, { method: "DELETE" });
    state.plan = null;
    state.editing = false;
    state.dirty = false;
    state.pendingId = null;
    $("plan-result").hidden = true;
    $("delete-plan").hidden = true;
    showDirectory();
    await loadPlans();
    notify("计划已删除，历史数据保留在数据库中。");
  } catch (error) {
    report(error);
  } finally {
    setBusy(false);
  }
}

function canLeavePlan() {
  return !state.dirty || window.confirm("放弃本次未保存的计划配置？已保存的计划不会改变。");
}

function updatePlanMode() {
  const editing = state.editing;
  $("plan-form").hidden = !editing;
  $("wizard-nav").hidden = !editing;
  $("plan-record-view").hidden = editing || !state.plan;
  $("edit-plan").hidden = editing || !state.plan;
  $("edit-plan").disabled = state.busy || Boolean(state.pendingId) || state.plan?.status === "running";
  $("cancel-plan").hidden = !editing;
  $("cancel-plan").disabled = state.busy || Boolean(state.pendingId);
  $("read-pending").hidden = !state.pendingId;
  $("delete-plan").hidden = !state.plan;
  $("delete-plan").disabled = state.busy || state.plan?.status === "running";
  $("plan-mode").textContent = editing ? (state.plan ? "调整配置，生成新计划" : "新建计划") : "查看计划";
  if (editing) {
    $("plan-title").textContent = state.plan ? "调整计划配置" : "新建计划";
    if (!state.plan) setBadge("未生成");
    else if (state.plan.status === "failed" || state.plan.status === "running") setStatusBadge(state.plan.status);
    else setBadge("调整配置中", "info");
    $("result-state").textContent = state.plan ? "当前显示的是上一次生成结果，仅供对照" : "";
  }
  if (!editing && state.plan) {
    const plan = state.plan;
    renderRecord($("plan-record-view"), {
      title: plan.inputs.direction || "面试计划",
      meta: `${statusLabels[plan.status] || plan.status} · 简历 v${plan.source_version} · ${plan.provider === "rule" ? "规则模式" : "模型模式"}`,
      fields: [
        ["预计时长", `${plan.inputs.duration_minutes} 分钟`],
        ["项目题时间比例", `${Math.round(plan.inputs.project_ratio * 100)}%（其余分给题库题）`],
        ["追问边界", plan.inputs.max_total_followups ? `总上限 ${plan.inputs.max_total_followups} 次` : "自动"],
        ["选用资料", `${plan.inputs.project_ids.length} 个已核对项目 · ${plan.inputs.question_ids.length} 道候选题目`],
        ["确认时间", plan.confirmed_at ? formatDate(plan.confirmed_at) : "尚未确认"],
      ],
    });
  }
  refreshConfirmState();
}

/* ---------- 事件 ---------- */

$("edit-plan").addEventListener("click", () => {
  if (state.busy || !state.plan) return;
  state.editing = true;
  state.dirty = false;
  state.step = 1;
  state.maxStep = 4;
  updatePlanMode();
  updateWizard();
  goStep(1);
});
$("adjust-config").addEventListener("click", () => $("edit-plan").click());
$("cancel-plan").addEventListener("click", async () => {
  if (state.busy || !canLeavePlan()) return;
  if (state.plan) {
    const plan = state.plan;
    setBusy(true);
    try {
      await loadOptions(plan.resume_id, plan.source_version, { selection: plan.inputs });
      fillForm(plan.inputs);
      state.dirty = false;
      state.editing = false;
      state.step = 4;
      renderPlan(plan);
      updatePlanMode();
      updateWizard();
    } catch (error) { report(error); }
    finally {
      setBusy(false);
      if (!state.editing) $("edit-plan").focus();
    }
  } else {
    state.editing = false;
    state.dirty = false;
    showDirectory();
    $("new-plan").focus();
  }
});
$("back-to-plans").addEventListener("click", async () => {
  if (state.busy || !canLeavePlan()) return;
  state.editing = false;
  state.dirty = false;
  state.plan = null;
  $("plan-result").hidden = true;
  showDirectory();
  try { await loadPlans(); } catch (error) { report(error); }
  if (state.pendingId) notify("上一份计划的结果尚未确定，可在列表重新打开该计划读取结果。");
  $("new-plan").focus();
});
$("new-plan").addEventListener("click", newPlan);
$("welcome-plan").addEventListener("click", newPlan);
$("refresh-plans").addEventListener("click", async () => {
  try { await loadPlans(); } catch (error) { report(error); }
});
$("plan-previous").addEventListener("click", async () => {
  state.planOffset = Math.max(0, state.planOffset - pageSize);
  try { await loadPlans(); } catch (error) { report(error); }
});
$("plan-next").addEventListener("click", async () => {
  state.planOffset += pageSize;
  try { await loadPlans(); } catch (error) { report(error); }
});
$("dismiss-message").addEventListener("click", () => { $("message").hidden = true; });
$("plan-form").addEventListener("submit", generate);
$("wizard-next").addEventListener("click", advance);
$("wizard-previous").addEventListener("click", () => goStep(state.step - 1));
for (const button of document.querySelectorAll("[data-plan-step]")) {
  button.addEventListener("click", () => jumpTo(Number(button.dataset.planStep)));
}
$("wizard-steps-toggle").addEventListener("click", () => {
  const open = !$("wizard-nav").classList.contains("steps-open");
  $("wizard-nav").classList.toggle("steps-open", open);
  $("wizard-steps-toggle").setAttribute("aria-expanded", String(open));
});
$("plan-form").addEventListener("input", (event) => {
  if (event.target.id === "ratio") $("ratio-value").textContent = `${event.target.value}%`;
  markDirty();
  if (state.step === 4) renderReviewSummary();
  else if (state.step === 3) updateCounts();
});
$("resume-select").addEventListener("change", async () => {
  setBusy(true);
  try {
    await loadOptions($("resume-select").value, null, { reset: true });
    markDirty();
  } catch (error) { report(error); }
  finally { setBusy(false); }
});
$("version-select").addEventListener("change", async () => {
  setBusy(true);
  try {
    await loadOptions($("resume-select").value, Number($("version-select").value), { reset: true });
    markDirty();
  } catch (error) { report(error); }
  finally { setBusy(false); }
});
$("project-all").addEventListener("click", () => {
  state.selectedProjects = new Set(state.options.projects.slice(0, state.options.limits.max_projects).map((item) => item.id));
  renderPickers();
  markDirty();
});
$("project-none").addEventListener("click", () => {
  state.selectedProjects = new Set();
  renderPickers();
  markDirty();
});
$("question-all").addEventListener("click", () => {
  state.selectedQuestions = randomSample(
    state.options.questions.map((item) => item.id),
    state.options.limits.max_questions,
  );
  renderPickers();
  markDirty();
  notify(`已随机选用 ${state.selectedQuestions.size} 道启用题目，可在上方逐题调整。`);
});
$("question-none").addEventListener("click", () => {
  state.selectedQuestions = new Set();
  renderPickers();
  markDirty();
});
$("read-pending").addEventListener("click", readPending);
$("confirm-plan").addEventListener("click", confirmPlan);
$("delete-plan").addEventListener("click", deletePlan);
$("reload-plan").addEventListener("click", async () => {
  if (!state.plan) return;
  try {
    const plan = await request(`${base}/${state.plan.id}`);
    state.plan = plan;
    renderPlan(plan);
    updatePlanMode();
  } catch (error) { report(error); }
});
window.addEventListener("beforeunload", (event) => {
  if (!state.dirty && !state.busy && !state.pendingId) return;
  event.preventDefault();
  event.returnValue = "";
});
for (const link of document.querySelectorAll(".app-brand, .app-navigation a")) {
  link.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (state.busy || !canLeavePlan()) event.preventDefault();
  });
}

(async function init() {
  try {
    await loadOptions(null, null, { reset: true, resetQuestions: true });
    await loadPlans();
    $("connection").textContent = "资料已就绪";
    renderPlanSubtitle(null);
    updateWizard();
  } catch (error) {
    $("connection").className = "connection error";
    $("connection").textContent = "读取失败";
    report(error);
  }
})();
