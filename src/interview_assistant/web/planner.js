const $ = (id) => document.getElementById(id);
const base = "/interview-plans";
const pageSize = 10;
const state = {
  options: null, plans: [], planTotal: 0, planOffset: 0,
  plan: null, selectedProjects: null, selectedQuestions: null,
  pendingId: null, busy: false, dirty: false, progressTimer: null,
};

const statusLabels = { running: "生成中", ready: "待确认", confirmed: "已确认", failed: "失败" };
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

const planContractVersion = "planner.v2";

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

function refreshConfirmState(plan = state.plan) {
  const ready = Boolean(plan) && plan.status === "ready";
  const outdated = ready && plan.schema_version !== planContractVersion;
  const drift = ready && !outdated && configDrift();
  $("confirm-plan").hidden = !ready;
  $("confirm-plan").disabled = state.busy || outdated || drift;
  const hint = $("config-drift");
  hint.hidden = !outdated && !drift;
  if (outdated) {
    hint.textContent = "此计划由旧版规则生成，缺少运行时资料依据，无法确认；请按当前输入重新生成。";
  } else if (drift) {
    hint.textContent = "当前配置与这份结果不一致（或资料已变化）。请重新生成后再确认；旧结果仍可回看。";
  }
}

function markDirty() {
  state.dirty = true;
  if (state.plan) refreshConfirmState();
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
  // 生成期间锁定输入，避免结果对应不上界面；待查询入口保持可用。
  for (const node of $("plan-form").querySelectorAll("input, select, button")) {
    if (node.id === "read-pending") continue;
    node.disabled = value;
  }
  $("new-plan").disabled = value;
  refreshConfirmState();
}

function startProgress() {
  const started = Date.now();
  const line = $("generate-progress");
  line.hidden = false;
  line.textContent = "已提交规划请求，模型模式通常需要数十秒；输入已保留，不会自动重试。";
  state.progressTimer = setInterval(() => {
    const seconds = Math.round((Date.now() - started) / 1000);
    line.textContent = `正在生成计划… 已等待 ${seconds} 秒。输入已保留，不会自动重试收费请求。`;
  }, 1000);
}

function stopProgress() {
  if (state.progressTimer) clearInterval(state.progressTimer);
  state.progressTimer = null;
  $("generate-progress").hidden = true;
}

function showWorkspace() {
  $("planner-welcome").hidden = true;
  $("planner-workspace").hidden = false;
}

function updateCounts() {
  $("project-selected-count").textContent = String(state.selectedProjects?.size ?? 0);
  $("question-selected-count").textContent = String(state.selectedQuestions?.size ?? 0);
}

function renderResumes() {
  const options = state.options;
  const select = $("resume-select");
  select.replaceChildren();
  if (!options.resumes.length) {
    const item = element("option", "暂无简历，请先在「简历与项目」创建");
    item.value = "";
    select.append(item);
  }
  for (const resume of options.resumes) {
    const item = element("option", `${resume.name} · 最新 v${resume.content_version}${resume.is_active ? "" : "（已停用）"}`);
    item.value = resume.id;
    select.append(item);
  }
  if (options.selected_resume_id) select.value = options.selected_resume_id;
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
    projectBox.append(element("p", "所选简历版本下没有已核对项目。请在「简历与项目」勾选「我已核对」并保存。", "picker-empty"));
  }
  for (const project of options.projects) {
    const label = element("label", null, "picker-item");
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
    const text = element("span", project.name);
    text.append(element("small", `v${project.source_version} · ${project.fact_count} 条事实 · ${project.tech_stack.join("、") || "未填技术栈"}`));
    label.append(input, text);
    projectBox.append(label);
  }

  if (!options.questions.length) {
    questionBox.append(element("p", "没有启用题目。请在「个人题库」启用题目后再生成计划。", "picker-empty"));
  }
  for (const question of options.questions) {
    const label = element("label", null, "picker-item");
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
    const text = element("span", question.question_text || "（题干为空）");
    text.append(element("small", `内容版本 v${question.content_version}`));
    label.append(input, text);
    questionBox.append(label);
  }
  $("question-hint").textContent = `当前共有 ${options.question_total} 道启用题目，按导入顺序列出；默认从中随机选用 ${maxQuestions} 道，可点「随机重选」重新抽取或逐题调整，计划会固化本次选择。`;
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

function renderPlanList() {
  const list = $("plan-list");
  list.replaceChildren();
  $("plan-count").textContent = String(state.planTotal);
  $("plan-page").textContent = `第 ${Math.floor(state.planOffset / pageSize) + 1} 页`;
  $("plan-previous").disabled = state.planOffset <= 0;
  $("plan-next").disabled = state.planOffset + pageSize >= state.planTotal;
  if (!state.plans.length) {
    list.append(element("p", "还没有计划，先在右侧配置并生成。", "list-placeholder"));
    return;
  }
  for (const item of state.plans) {
    const button = element("button", null, `plan-card${state.plan?.id === item.id ? " selected" : ""}`);
    button.type = "button";
    button.append(element("span", statusLabels[item.status] || item.status, `plan-badge ${item.status}`));
    button.append(element("strong", item.direction || "未填写考察方向"));
    button.append(element("small", `${formatDate(item.created_at)} · ${item.duration_minutes} 分钟 · ${item.objective_count} 个目标 · ${item.provider === "rule" ? "规则模式" : "模型模式"}`));
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

function fillForm(inputs) {
  $("direction").value = inputs.direction || "";
  $("duration").value = String(inputs.duration_minutes);
  $("ratio").value = String(Math.round(inputs.project_ratio * 100));
  $("ratio-value").textContent = `${$("ratio").value}%`;
  $("followups").value = String(inputs.max_total_followups);
  $("mode").value = inputs.mode;
}

function renderResultHeader(plan) {
  $("plan-title").textContent = plan.status === "confirmed" ? "已确认计划" : "计划详情";
  $("plan-status").textContent = statusLabels[plan.status] || plan.status;
  $("result-state").textContent = `简历 v${plan.source_version} · ${plan.provider === "rule" ? "规则模式" : `模型 ${plan.model || "Qwen"}`}`;
  $("plan-meta").textContent = plan.confirmed_at
    ? `已于 ${formatDate(plan.confirmed_at)} 确认保存，资料引用已固定。`
    : "目标时长仅作节奏参考，到时不强制结束。";
  $("delete-plan").hidden = false;
  $("generate-plan").textContent = "按当前输入重新生成 ↗";
  $("plan-conflict").hidden = true;
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
  const details = element("details");
  details.append(element("summary",
    `核对引用明细（${objective.project_fact_ids.length} 条事实 / ${objective.question_ids.length} 道题）`));
  const box = element("div");
  let count = 0;
  for (const id of objective.project_fact_ids) {
    const item = factIndex.get(id);
    const line = element("p", null, "reference-item");
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
    const line = element("p", null, "reference-item");
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
    return;
  }
  const result = plan.result;
  if (!result) {
    summary.append(element("div", "此计划没有可展示的结果，请重新生成。", "result-error"));
    renderNotes({ limitations: [] });
    return;
  }
  summary.textContent = result.session_summary || "依据所选资料的面试计划";
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
    const card = element("div", null, "budget-card");
    card.append(element("small", title), element("strong", value), element("small", note));
    $("result-budgets").append(card);
  }
  for (const objective of result.objectives) {
    const card = element("article", null, "objective-card");
    const heading = element("h5");
    const sourceClass = objective.source_preference === "project" ? "objective-badge project" : "objective-badge";
    heading.append(
      element("span", objective.id, "objective-badge"),
      element("span", objective.source_preference === "project" ? "项目题" : "题库题", sourceClass),
      element("span", priorityLabels[objective.priority] || objective.priority, "objective-badge"),
      document.createTextNode(objective.topic),
    );
    card.append(heading);
    if (objective.observable_evidence.length) {
      card.append(element("p", `可观察证据：${objective.observable_evidence.join("；")}`, "objective-evidence"));
    }
    card.append(element("p", `时间预算 ${formatSeconds(objective.time_budget_sec)} · 追问上限 ${objective.max_followups}${objective.completion_rule ? ` · ${objective.completion_rule}` : ""}`));
    card.append(element("div", `事实引用 ${objective.project_fact_ids.length} 条 · 题目引用 ${objective.question_ids.length} 道（均为生成时的固定版本）`, "objective-refs"));
    card.append(buildReferenceDetails(objective, plan));
    const context = (result.runtime_context || {})[objective.id];
    if (context) {
      const runtime = element("details");
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

async function selectPlan(id) {
  if (state.busy) return;
  try {
    const plan = await request(`${base}/${id}`);
    state.plan = plan;
    showWorkspace();
    await loadOptions(plan.resume_id, plan.source_version, { selection: plan.inputs });
    fillForm(plan.inputs);
    renderPlan(plan);
    renderPlanList();
    state.dirty = false;
    $("read-pending").hidden = !state.pendingId;
    setBusy(false);
  } catch (error) {
    report(error);
  }
}

async function newPlan() {
  if (state.busy) return;
  state.plan = null;
  showWorkspace();
  $("plan-result").hidden = true;
  $("plan-title").textContent = "新建计划";
  $("plan-status").textContent = "未生成";
  $("generate-plan").textContent = "生成计划 ↗";
  $("delete-plan").hidden = true;
  $("read-pending").hidden = !state.pendingId;
  $("plan-conflict").hidden = true;
  $("direction").value = "";
  $("duration").value = "45";
  $("ratio").value = "50";
  $("ratio-value").textContent = "50%";
  $("followups").value = "0";
  try {
    await loadOptions(null, null, { reset: true, resetQuestions: true });
    renderPlanList();
    state.dirty = false;
    notify("已载入可选资料。选择项目与题目后生成计划。");
  } catch (error) {
    report(error);
  }
}

async function generate(event) {
  event.preventDefault();
  if (state.busy) return;
  const resumeId = $("resume-select").value;
  const version = Number($("version-select").value);
  if (!resumeId || !Number.isInteger(version) || version < 1) {
    notify("请先选择简历与简历版本", true);
    return;
  }
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
    state.pendingId = null;
    state.dirty = false;
    renderPlan(plan);
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
    // 仍在生成时保留待查询标识，允许稍后继续读取同一任务。
    if (plan.status !== "running") state.pendingId = null;
    $("read-pending").hidden = !state.pendingId;
    if (plan.status !== "running") state.dirty = false;
    renderPlan(plan);
    await loadPlans();
  } catch (error) {
    report(error);
  } finally {
    setBusy(false);
  }
}

async function confirmPlan() {
  if (!state.plan || state.plan.status !== "ready" || state.busy) return;
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
    await loadPlans();
    notify("计划已确认保存，资料引用已固定。");
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
    $("plan-result").hidden = true;
    $("delete-plan").hidden = true;
    await loadPlans();
    notify("计划已删除，历史数据保留在数据库中。");
  } catch (error) {
    report(error);
  } finally {
    setBusy(false);
  }
}

$("new-plan").addEventListener("click", newPlan);
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
$("plan-form").addEventListener("input", (event) => {
  if (event.target.id === "ratio") $("ratio-value").textContent = `${event.target.value}%`;
  markDirty();
});
$("resume-select").addEventListener("change", async () => {
  try {
    await loadOptions($("resume-select").value, null, { reset: true });
    markDirty();
  } catch (error) { report(error); }
});
$("version-select").addEventListener("change", async () => {
  try {
    await loadOptions($("resume-select").value, Number($("version-select").value), { reset: true });
    markDirty();
  } catch (error) { report(error); }
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
  notify(`已随机选用 ${state.selectedQuestions.size} 道启用题目，可在下方逐题调整。`);
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
  } catch (error) { report(error); }
});
window.addEventListener("beforeunload", (event) => {
  if (!state.dirty || state.busy) return;
  event.preventDefault();
  event.returnValue = "";
});

(async function init() {
  try {
    await loadOptions(null, null, { reset: true });
    await loadPlans();
    $("connection").textContent = "资料已就绪";
  } catch (error) {
    $("connection").textContent = "读取失败";
    report(error);
  }
})();
