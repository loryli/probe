const $ = (id) => document.getElementById(id);
const base = "/interviews";
const pageSize = 10;
const state = {
  options: null, sessions: [], total: 0, offset: 0,
  session: null, busy: false, timer: null, waitTimer: null, fetchedAt: 0,
  stream: null, live: false, report: null, reportBusy: false,
  context: 0, panel: null, returnFocus: null, runtime: null, referencesBusy: false,
  answerSelection: null,
};

const statusLabels = { active: "进行中", paused: "已暂停", completed: "已结束" };
const objectiveLabels = { pending: "待考察", active: "进行中", covered: "已覆盖", skipped: "已跳过" };
const sourceLabels = { project: "项目题", question_bank: "题库题" };
const priorityLabels = { high: "高", medium: "中", low: "低" };
const decisionLabels = { follow_up: "追问", continue: "同目标续问", switch: "切换目标", complete: "收尾" };

const formatDate = (value) => new Date(value).toLocaleString("zh-CN", { hour12: false });
const formatSeconds = (value) => {
  const seconds = Math.max(0, Number(value) || 0);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
};

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
    throw new ApiError("连接失败或请求超时。回答可能已经保存，刷新本场记录即可确认。", 0);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const fallback = {
      404: "场次或计划已不存在，请刷新列表。",
      409: "状态已变化，请刷新本场记录后重试。",
      422: "输入不符合要求，请检查后重试。",
      503: "服务暂不可用，请确认数据库与模型配置。",
    };
    throw new ApiError(
      typeof detail?.message === "string" ? detail.message :
        fallback[response.status] || "操作未完成，请稍后重试。",
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
  notify(error instanceof ApiError ? error.message : "操作未完成，请稍后重试。", true);
}

function setBusy(value) {
  state.busy = value;
  $("new-session").disabled = value;
  $("answer-text").disabled = value;
  for (const node of $("interview-room").querySelectorAll("button")) node.disabled = value;
  $("start-session").disabled = value || !selectedPlanUsable();
  if (state.session) renderControls(state.session);
}

// 每轮都要等一次模型调用，把等待时间显式呈现出来，而不是只留一句静态提示。
function startWait(message) {
  stopWait();
  const started = Date.now();
  const tick = () => {
    const text = `${message}已等待 ${((Date.now() - started) / 1000).toFixed(1)}s`;
    $("answer-hint").textContent = text;
    // 没有待答问题时回答框是隐藏的，等待信息还要落在底部备注上。
    $("room-note").textContent = text;
  };
  tick();
  state.waitTimer = setInterval(tick, 200);
}

function stopWait() {
  if (state.waitTimer) clearInterval(state.waitTimer);
  state.waitTimer = null;
  $("answer-hint").textContent = "";
  $("room-note").textContent = "回答先落库，面试官的话随之逐字出现。";
}

// 回答提交后由服务端后台决策，这里订阅进度；拿到可作答状态就关流。
function closeStream() {
  if (state.stream) state.stream.close();
  state.stream = null;
  if (state.session) renderControls(state.session);
}

function subscribe(id) {
  closeStream();
  const stream = new EventSource(`${base}/${id}/events`);
  state.stream = stream;
  stream.addEventListener("state", async (event) => {
    let updated;
    try { updated = JSON.parse(event.data); } catch { return; }
    if (!state.session || state.session.id !== id) return;
    state.session = updated;
    state.fetchedAt = Date.now();
    renderSession(updated);
    const settled = updated.status !== "active"
      || Boolean(updated.current_turn) || Boolean(updated.error_code);
    if (!settled) return;
    closeStream();
    stopWait();
    await loadSessions();
    if (updated.error_code) {
      notify(`下一步没有生成成功：${updated.error_message || updated.error_code}。可点「重试下一步」。`, true);
    }
  });
  stream.addEventListener("error", () => {
    // 服务端正常关闭也会触发 error；只有仍挂在流上时才需要兜底。
    if (state.stream !== stream) return;
    closeStream();
    pollUntilSettled(id);
  });
  if (state.session) renderControls(state.session);
}

// SSE 不可用（代理缓冲、断线）时退化为轮询，避免用户一直停在等待态。
async function pollUntilSettled(id) {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    if (!state.session || state.session.id !== id) return;
    try {
      const updated = await request(`${base}/${id}`);
      if (updated.status !== "active" || updated.current_turn || updated.error_code) {
        state.session = updated;
        state.fetchedAt = Date.now();
        stopWait();
        renderSession(updated);
        await loadSessions();
        return;
      }
    } catch { return; }
  }
  stopWait();
  notify("下一步生成超时，可点「重试下一步」继续。", true);
}

function showWelcome() {
  $("interview-welcome").hidden = false;
  $("start-panel").hidden = true;
  $("interview-room").hidden = true;
  const note = $("welcome-note");
  if (!state.options) return;
  note.textContent = planOptions().some((item) => item.usable)
    ? "点击右上角「新建面试」选择计划开始；回答提交后先落库，面试官的话随即逐字出现。"
    : "已有的已确认计划都来自旧版规则，缺少模拟面试所需的运行依据；请先到「面试计划」页按当前配置重新生成并确认。";
}

function showStart() {
  if (state.busy || !canLeaveDraft()) return;
  resetSessionView();
  closeStream();
  stopWait();
  state.session = null;
  clearInterval(state.timer);
  $("answer-text").value = "";
  $("interview-welcome").hidden = true;
  $("interview-room").hidden = true;
  $("start-panel").hidden = false;
  renderPlanOptions();
  $("plan-select").focus();
}

function showRoom() {
  $("interview-welcome").hidden = true;
  $("start-panel").hidden = true;
  $("interview-room").hidden = false;
}

async function loadOptions() {
  state.options = await request(`${base}/options`);
  renderPlanOptions();
}

function planOptions() {
  return state.options?.plans || [];
}

function selectedPlan() {
  const value = $("plan-select").value;
  return planOptions().find((item) => item.id === value) || null;
}

function selectedPlanUsable() {
  const plan = selectedPlan();
  return Boolean(plan && plan.usable);
}

function planLabel(plan) {
  return `${plan.direction || "未填写方向"} · ${plan.duration_minutes} 分钟 · `
    + `${plan.objective_count} 个目标 · ${formatDate(plan.confirmed_at || plan.created_at)}`;
}

function renderPlanOptions() {
  const options = state.options;
  if (!options) return;
  const select = $("plan-select");
  const previous = select.value;
  const usable = planOptions().filter((item) => item.usable);
  const unusable = planOptions().filter((item) => !item.usable);
  select.replaceChildren();
  if (!usable.length) {
    select.append(element("option", planOptions().length
      ? "— 没有可用的计划 —" : "— 还没有已确认的计划 —"));
    select.disabled = true;
  } else {
    select.disabled = false;
    for (const plan of usable) {
      const option = element("option", planLabel(plan));
      option.value = plan.id;
      select.append(option);
    }
    // 旧版计划保留展示但不可选，避免「选得到、点不动」的困惑。
    for (const plan of unusable) {
      const option = element("option", `${planLabel(plan)} · 需重新生成`);
      option.value = plan.id;
      option.disabled = true;
      option.title = plan.reason || "旧版计划缺少新版运行依据";
      select.append(option);
    }
    if (previous && usable.some((item) => item.id === previous)) select.value = previous;
    else select.selectedIndex = 0;
  }
  const hint = $("plan-hint");
  hint.classList.remove("error");
  if (!planOptions().length) {
    hint.classList.add("error");
    hint.textContent = "还没有已确认的计划：先到「面试计划」页生成并确认一份计划，再回来开始面试。";
  } else if (!usable.length) {
    hint.classList.add("error");
    hint.textContent = `这 ${unusable.length} 份计划由旧版规则生成，缺少新版运行依据，`
      + "不能开始面试。请按当前配置重新生成并确认一份计划。";
  } else if (unusable.length) {
    hint.textContent = `只列出已确认的计划；开始后资料与目标会冻结。`
      + `另有 ${unusable.length} 份旧版计划不能使用（已置灰），需重新生成并确认。`;
  } else {
    hint.textContent = "只列出已确认的计划；开始后资料与目标会冻结，不再跟随资料修改。";
  }
  $("plan-fix").hidden = usable.length > 0;
  $("start-session").disabled = state.busy || !usable.length;
  const model = options.model || {};
  $("model-status").textContent = model.configured
    ? `模型 ${model.model || "—"} 已配置；可切换规则模式，不调用模型。`
    : `模型未配置（缺少 ${(model.missing || []).join("、") || "密钥"}）；可先用规则模式走完整场次。`;
}

async function loadSessions() {
  const data = await request(`${base}?limit=${pageSize}&offset=${state.offset}`);
  state.sessions = data.items;
  state.total = data.total;
  renderSessionList();
}

function renderSessionList() {
  const list = $("session-list");
  list.replaceChildren();
  $("session-count").textContent = String(state.total);
  if (!state.sessions.length) {
    list.append(element("p", "还没有面试记录。确认计划后开始第一场。", "list-placeholder"));
  }
  for (const session of state.sessions) {
    const card = element("button", null, "session-card");
    card.type = "button";
    card.classList.toggle("selected", state.session?.id === session.id);
    const title = element("strong", session.direction || "未填写方向");
    const line = element("small",
      `${statusLabels[session.status] || session.status} · ${session.mode === "model" ? "Qwen" : "规则"} · `
      + `${session.answer_count} 轮回答 · 目标 ${session.objective_done}/${session.objective_total}`);
    const time = element("small", `${formatDate(session.created_at)} · 用时 ${formatSeconds(session.elapsed_sec)}`);
    card.append(title, line, time);
    card.addEventListener("click", () => { openSession(session.id); });
    list.append(card);
  }
  $("session-page").textContent = `第 ${Math.floor(state.offset / pageSize) + 1} 页`;
  $("session-previous").disabled = state.offset === 0;
  $("session-next").disabled = state.offset + pageSize >= state.total;
}

async function openSession(id) {
  if (state.busy || !canLeaveDraft()) return;
  setBusy(true);
  closeStream();
  stopWait();
  try {
    const session = await request(`${base}/${id}`);
    resetSessionView();
    state.session = session;
    state.fetchedAt = Date.now();
    const context = state.context;
    $("answer-text").value = "";
    showRoom();
    renderSession(session);
    $("room-title").focus();
    renderSessionList();
    startTimer();
    // 已结束的场次顺带读一次复盘；没有生成过（404）就保持无报告状态。
    if (session.status === "completed") {
      request(`${base}/${id}/report`).then((report) => {
        if (state.context === context && state.session?.id === session.id) {
          state.report = report;
          renderReportArea(session);
        }
      }).catch(() => {});
    }
    // 刷新时若上一轮决策还在后台跑，直接续订进度，而不是让用户点重试。
    if (session.status === "active" && session.pending_turn_id && !session.current_turn) {
      startWait("上一条回答已保存，正在等待下一步…");
      subscribe(session.id);
    }
  } catch (error) {
    report(error);
  } finally {
    setBusy(false);
  }
}

async function startSession(event) {
  event.preventDefault();
  if (state.busy) return;
  const plan = selectedPlan();
  if (!plan || !plan.usable) {
    notify(plan?.reason
      ? `${plan.reason}。请到「面试计划」页重新生成并确认后再开始。`
      : "请先选择一份可用的已确认计划。", true);
    return;
  }
  const planId = plan.id;
  setBusy(true);
  // 开场只为一个目标生成基础问题，等待与单轮出题同量级。
  $("connection").textContent = "正在生成首问";
  $("start-session").textContent = "正在生成首问…";
  try {
    const session = await request(base, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        request_id: crypto.randomUUID(),
        plan_id: planId,
        mode: $("session-mode").value,
      }),
      timeoutMs: 180000,
    });
    resetSessionView();
    state.session = session;
    state.fetchedAt = Date.now();
    showRoom();
    renderSession(session);
    $("room-title").focus();
    startTimer();
    $("connection").textContent = "已连接";
    await loadSessions();
    if (session.error_code) {
      notify(`首问没有生成成功：${session.error_message || session.error_code}。场次已保存，可点「继续生成下一问」。`, true);
    }
  } catch (error) {
    report(error);
  } finally {
    $("start-session").textContent = "开始面试 ↗";
    setBusy(false);
  }
}

async function submitAnswer() {
  const session = state.session;
  if (!session || !session.current_turn || state.busy) return;
  const text = $("answer-text").value.trim();
  if (!text) {
    notify("先写下你的回答再提交。", true);
    $("answer-text").focus();
    return;
  }
  closeStream();
  state.live = true;
  setBusy(true);
  startWait("正在保存回答…");
  let bubble = null;
  let settled = false;
  try {
    await readAnswerStream(session, text, {
      ack(read) {
        // 回答已落库：先确认保存，再开始逐字接收面试官的话。
        state.session = read;
        state.fetchedAt = Date.now();
        $("answer-text").value = "";
        renderSession(read);
        bubble = beginBubble();
        startWait("面试官正在说…");
      },
      delta(payload) {
        if (!bubble) {
          bubble = beginBubble();
          // 第一个字到了就不必再显示等待秒数。
          stopWait();
        }
        bubble.textNode.textContent += payload.text || "";
        const box = $("current-question");
        box.scrollTop = box.scrollHeight;
      },
      state(read) {
        settled = true;
        if (bubble) { bubble.card.remove(); bubble = null; }
        state.session = read;
        state.fetchedAt = Date.now();
        stopWait();
        renderSession(read);
        loadSessions().catch(report);
        if (read.error_code) {
          notify(`回答已保存。下一步没有生成成功：${read.error_message || read.error_code}。可点「重试下一步」。`, true);
        }
      },
      error(payload) {
        settled = true;
        notify(`回答已保存，但${payload.message || "下一步没有生成成功"}。可点「重试下一步」。`, true);
      },
    });
  } catch (error) {
    // 流建立失败时回答可能已经保存：回读一次再提示，避免用户重复提交。
    report(error);
  } finally {
    if (bubble) bubble.card.remove();
    state.live = false;
    stopWait();
    setBusy(false);
    if (!settled) await refreshQuietly();
  }
}

// 提交回答并逐帧读取：ack（已保存）、delta（面试官的话）、state（最终快照）、error。
async function readAnswerStream(session, text, handlers) {
  let response;
  try {
    response = await fetch(`${base}/${session.id}/answers/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({
        request_id: crypto.randomUUID(),
        turn_id: session.current_turn.id,
        answer_text: text,
      }),
      signal: AbortSignal.timeout(300000),
    });
  } catch {
    throw new ApiError("连接失败或请求超时。回答可能已经保存，刷新本场记录即可确认。", 0);
  }
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    throw new ApiError(
      typeof detail?.message === "string" ? detail.message : "提交失败，请稍后重试。",
      response.status, detail?.code,
    );
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n\n")) >= 0) {
      dispatchFrame(buffer.slice(0, index), handlers);
      buffer = buffer.slice(index + 2);
    }
  }
  if (buffer.trim()) dispatchFrame(buffer, handlers);
}

function dispatchFrame(frame, handlers) {
  let event = "message";
  const data = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return;
  let payload;
  try { payload = JSON.parse(data.join("\n")); } catch { return; }
  const handler = handlers[event];
  if (typeof handler === "function") handler(payload);
}

// 面试官开口前的占位气泡：逐字追加，结束时被真实轮次替换。
function beginBubble() {
  const box = $("current-question");
  box.replaceChildren();
  const card = element("article", null, "turn question streaming");
  const head = element("div", null, "turn-head");
  head.append(element("span", "面试官正在说"));
  const textNode = element("p", "", "turn-text");
  card.append(head, textNode);
  box.append(card);
  box.scrollTop = box.scrollHeight;
  return { card, textNode };
}

async function retryStep() {
  const session = state.session;
  if (!session || state.busy) return;
  closeStream();
  setBusy(true);
  startWait("正在续接…");
  try {
    const updated = await request(`${base}/${session.id}/retry`, {
      method: "POST",
      timeoutMs: 180000,
    });
    state.session = updated;
    state.fetchedAt = Date.now();
    renderSession(updated);
    await loadSessions();
    if (updated.error_code) notify(`${updated.error_message || updated.error_code}`, true);
  } catch (error) {
    report(error);
  } finally {
    stopWait();
    setBusy(false);
  }
}

async function control(action, confirmText) {
  const session = state.session;
  if (!session || state.busy) return;
  if (confirmText && !window.confirm(confirmText)) return;
  closeStream();
  stopWait();
  setBusy(true);
  try {
    const updated = await request(`${base}/${session.id}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: session.revision }),
      timeoutMs: 180000,
    });
    if (action === "skip") $("answer-text").value = "";
    state.session = updated;
    state.fetchedAt = Date.now();
    renderSession(updated);
    await loadSessions();
    if (updated.error_code) notify(`${updated.error_message || updated.error_code}`, true);
  } catch (error) {
    report(error);
    await refreshQuietly();
  } finally {
    setBusy(false);
  }
}

async function refreshQuietly() {
  const session = state.session;
  if (!session) return;
  try {
    const updated = await request(`${base}/${session.id}`);
    // 回答已被服务端接收时清空草稿，避免重复提交到下一题。
    if (updated.answer_count > session.answer_count
        || updated.current_turn?.id !== session.current_turn?.id) $("answer-text").value = "";
    state.session = updated;
    state.fetchedAt = Date.now();
    renderSession(updated);
    await loadSessions();
  } catch { /* 保留当前画面，错误已在主流程提示 */ }
}

function startTimer() {
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    const session = state.session;
    const node = $("room-elapsed");
    if (!session || !node) return;
    const extra = session.status === "active" ? Math.floor((Date.now() - state.fetchedAt) / 1000) : 0;
    const strong = node.querySelector("strong");
    if (strong) strong.textContent = formatSeconds(session.elapsed_sec + extra);
  }, 1000);
}

function objectiveText(session, objectiveId) {
  const objective = session.objectives.find((item) => item.id === objectiveId);
  if (!objective) return objectiveId || "未关联目标";
  return `${objective.id} · ${objective.topic}`;
}

function renderSession(session) {
  $("room-status").textContent = statusLabels[session.status] || session.status;
  $("room-status").className = `save-state`;
  $("room-title").textContent = session.direction ? `面试 · ${session.direction.slice(0, 28)}` : "模拟面试";
  if (session.status === "completed") $("answer-text").value = "";
  renderMeta(session);
  renderObjectives(session);
  renderCurrentQuestion(session);
  renderConversation(session);
  if (state.panel === "references") renderReferences();
  renderControls(session);
  renderNotice(session);
  renderSummary(session);
  renderReportArea(session);
}

function renderMeta(session) {
  const node = $("room-meta");
  node.replaceChildren();
  const covered = session.objectives.filter((item) => item.status === "covered").length;
  const entries = [
    ["目标", `${covered}/${session.objectives.length} 已覆盖`],
    ["问答", `${session.answer_count} 轮 / ${session.question_count} 问`],
    ["追问", `${session.followups_used} 次`],
    ["方式", session.mode === "model" ? `Qwen${session.model ? ` · ${session.model}` : ""}` : "规则模式"],
    ["计划", `${Math.round(session.duration_target_sec / 60)} 分钟`],
    ["契约", session.prompt_version],
  ];
  for (const [label, value] of entries) {
    const span = element("span");
    span.append(element("small", `${label} `), element("strong", value));
    node.append(span);
  }
  $("room-elapsed").querySelector("strong").textContent = formatSeconds(session.elapsed_sec);
  $("workspace-progress").textContent = `${covered}/${session.objectives.length} 个目标已覆盖 · 已回答 ${session.answer_count} 轮`;
  const remaining = session.duration_target_sec - session.elapsed_sec;
  node.append(element("span", remaining >= 0
    ? `计划剩余 ${formatSeconds(remaining)}`
    : `已超计划 ${formatSeconds(-remaining)}`));
}

function renderObjectives(session) {
  const rail = $("objective-rail");
  rail.replaceChildren();
  for (const objective of session.objectives) {
    const chip = element("span", `${objective.id} · ${objective.topic}`, `rail-chip ${objective.status}`);
    chip.title = `${sourceLabels[objective.source]} · ${priorityLabels[objective.priority]}优先级 · `
      + `${objectiveLabels[objective.status]} · 已问 ${objective.asked} · 追问 ${objective.followups}/${objective.max_followups}`;
    chip.append(element("small", `${objectiveLabels[objective.status]} · 已问 ${objective.asked} · 追问 ${objective.followups}/${objective.max_followups}`));
    rail.append(chip);
  }
}

function renderConversation(session) {
  const box = $("conversation");
  box.replaceChildren();
  let questionIndex = 0;
  let answerIndex = 0;
  for (const turn of session.turns) {
    if (turn.role === "interviewer") {
      questionIndex += 1;
      if (turn.id === session.current_turn?.id) continue;
      const card = element("article", null, "turn question");
      const head = element("div", null, "turn-head");
      head.append(element("span", `第 ${questionIndex} 问`));
      if (turn.source) head.append(element("span", sourceLabels[turn.source] || turn.source, "room-badge"));
      if (turn.objective_id) head.append(element("span", objectiveText(session, turn.objective_id)));
      if (turn.is_followup) head.append(element("span", "追问", "room-badge turn-followup"));
      if (turn.skipped) head.append(element("span", "已跳过", "turn-skipped"));
      card.append(head, element("p", turn.text, "turn-text"));
      box.append(card);
      continue;
    }
    answerIndex += 1;
    const card = element("article", null, "turn answer");
    const head = element("div", null, "turn-head");
    head.append(element("span", `第 ${answerIndex} 轮回答`));
    if (turn.decision) {
      head.append(element("span", decisionLabels[turn.decision.action] || turn.decision.action, "room-badge"));
    }
    card.append(head, element("p", turn.text, "turn-text"));
    if (turn.decision) {
      const note = [
        turn.decision.observation ? `观察：${turn.decision.observation}` : "",
        turn.decision.reason ? `理由：${turn.decision.reason}` : "",
      ].filter(Boolean).join(" · ");
      if (note) card.append(element("p", note, "turn-note"));
    }
    box.append(card);
  }
  if (!box.children.length) {
    box.append(element("p", "已提交的问答会显示在这里。", "list-placeholder"));
  }
  box.scrollTop = box.scrollHeight;
}

function renderControls(session) {
  const active = session.status === "active";
  const paused = session.status === "paused";
  const completed = session.status === "completed";
  const hasQuestion = Boolean(session.current_turn);
  const streaming = Boolean(state.stream) || state.live;
  $("answer-box").hidden = !(active && hasQuestion);
  $("skip-objective").hidden = !(active && hasQuestion);
  $("pause-session").hidden = !active;
  $("resume-session").hidden = !paused;
  $("retry-step").hidden = !(active && !hasQuestion);
  $("retry-step").textContent = streaming
    ? "正在生成下一步…"
    : (session.pending_turn_id ? "重试下一步" : "继续生成下一问");
  $("retry-step").disabled = state.busy || streaming;
  $("end-session").hidden = completed;
  $("submit-answer").disabled = state.busy;
  $("pause-session").disabled = state.busy;
  $("resume-session").disabled = state.busy;
  $("skip-objective").disabled = state.busy;
  $("end-session").disabled = state.busy;
  $("room-note").textContent = completed
    ? "本场记录已保存，可回看每一轮问答。"
    : "回答先落库，面试官的话随之逐字出现。";
}

function renderNotice(session) {
  const notice = $("room-notice");
  notice.className = "room-notice";
  if (session.status === "completed") {
    notice.hidden = true;
    return;
  }
  if (session.status === "paused") {
    notice.hidden = false;
    notice.textContent = "已暂停。当前问题与已提交的回答都已保存，点「继续面试」接着答。";
    return;
  }
  if (!session.current_turn) {
    notice.hidden = false;
    if (session.pending_turn_id) {
      const waiting = !session.error_code;
      notice.className = waiting ? "room-notice" : "room-notice error";
      notice.textContent = session.error_code
        ? `回答已保存，但下一步没有生成成功：${session.error_message || session.error_code}。点「重试下一步」继续，不会重复保存回答。`
        : (state.live ? "回答已保存，面试官正在说…" : "回答已保存，正在等待下一步。");
    } else {
      notice.className = session.error_code ? "room-notice error" : "room-notice";
      notice.textContent = session.error_code
        ? `问题没有生成成功：${session.error_message || session.error_code}。点「继续生成下一问」重试。`
        : "当前没有待回答的问题，点「继续生成下一问」获取下一题。";
    }
    return;
  }
  if (session.error_code) {
    notice.hidden = false;
    notice.textContent = `${session.error_message || session.error_code}（当前问题仍可作答）`;
    return;
  }
  notice.hidden = true;
}

function renderSummary(session) {
  const node = $("room-summary");
  if (session.status !== "completed") {
    node.hidden = true;
    node.replaceChildren();
    return;
  }
  node.replaceChildren();
  node.hidden = false;
  const covered = session.objectives.filter((item) => item.status === "covered").length;
  const skipped = session.objectives.filter((item) => item.status === "skipped").length;
  node.append(element("h3", "本场已结束，记录已保存"));
  const grid = element("div", null, "summary-grid");
  const cards = [
    ["回答问题", `${session.answer_count} 轮`],
    ["提出问题", `${session.question_count} 个`],
    ["覆盖目标", `${covered}/${session.objectives.length}`],
    ["用时", formatSeconds(session.elapsed_sec)],
  ];
  for (const [label, value] of cards) {
    const card = element("div", null, "summary-card");
    card.append(element("strong", value), element("small", label));
    grid.append(card);
  }
  node.append(grid);
  if (session.completion_reason) node.append(element("p", session.completion_reason));
  if (skipped) node.append(element("p", `有 ${skipped} 个目标被跳过，未展开考察。`));
  const list = element("ul");
  for (const objective of session.objectives) {
    const state = objective.status === "covered" ? "已覆盖"
      : objective.status === "skipped" ? "已跳过"
        : objective.status === "active" ? "未完成" : "未开始";
    list.append(element("li", `${objective.id} · ${objective.topic} — ${state}（问 ${objective.asked} / 追问 ${objective.followups}）`));
  }
  node.append(list);
  const again = element("button", "再开一场 ↗", "button primary");
  again.type = "button";
  again.addEventListener("click", showStart);
  node.append(again);
}

const overallLabels = { solid: "整体扎实", mixed: "有亮点有短板", weak: "证据明显不足" };
const evaluationLabels = { covered: "已覆盖", partial: "部分覆盖", missed: "未覆盖" };

function renderReportArea(session) {
  const node = $("room-report");
  if (!node || !session || session.status !== "completed") {
    if (node) { node.hidden = true; node.replaceChildren(); }
    return;
  }
  node.hidden = false;
  node.replaceChildren();
  const report = state.report;
  if (state.reportBusy) {
    node.append(element("p", "正在复盘整场（约 15–40 秒）：逐目标评价、弱项与练习建议生成后会显示在这里。", "report-loading"));
    return;
  }
  if (report && report.status === "ready" && report.report) {
    renderReport(node, report, session);
    return;
  }
  if (report && report.status !== "ready") {
    node.append(element("h3", "复盘没有生成成功"));
    node.append(element("p", `${report.error_message || report.error_code || "生成失败"}。问答记录已保存，可以直接重试。`, "report-failed"));
    const retry = element("button", "重试生成复盘 ↗", "button");
    retry.type = "button";
    retry.addEventListener("click", generateReport);
    node.append(retry);
    return;
  }
  const intro = element("p", "让 Qwen 复盘整场问答：逐目标评价达成度、指出弱项与矛盾，并给出下次练习建议。题库题会对照你自己保存的标准答案。", "report-intro");
  const start = element("button", "生成复盘报告 ↗", "button primary");
  start.type = "button";
  start.addEventListener("click", generateReport);
  node.append(element("h3", "整场复盘"), intro, start);
}

function renderReport(node, report, session) {
  const content = report.report;
  const head = element("div", null, "report-head");
  const badge = element("span", overallLabels[content.overall] || content.overall, `report-badge ${content.overall}`);
  head.append(element("h3", "整场复盘"), badge);
  node.append(head);
  if (content.summary) node.append(element("p", content.summary, "report-summary"));
  const objectiveById = new Map(session.objectives.map((item) => [item.id, item]));
  if (content.evaluations.length) {
    const list = element("div", null, "report-objectives");
    for (const item of content.evaluations) {
      const objective = objectiveById.get(item.objective_id);
      const card = element("article", null, "report-card");
      const title = element("h4", `${item.objective_id} · ${objective ? objective.topic : "未知目标"}`);
      const stateBadge = element("span", evaluationLabels[item.status] || item.status, `report-badge small ${item.status}`);
      title.append(stateBadge);
      card.append(title);
      if (item.comment) card.append(element("p", item.comment));
      if (item.highlights.length) {
        const block = element("div", null, "report-points good");
        block.append(element("strong", "亮点"));
        for (const point of item.highlights) block.append(element("p", point));
        card.append(block);
      }
      if (item.gaps.length) {
        const block = element("div", null, "report-points bad");
        block.append(element("strong", "缺口"));
        for (const point of item.gaps) block.append(element("p", point));
        card.append(block);
      }
      if (item.turn_sequences.length) {
        card.append(element("small", `证据轮次：第 ${item.turn_sequences.join("、")} 轮回答`));
      }
      list.append(card);
    }
    node.append(list);
  }
  const sections = [
    ["weak_topics", "弱项主题"],
    ["suggestions", "下次练习建议"],
    ["risks", "矛盾与待核验"],
  ];
  for (const [key, title] of sections) {
    const items = content[key];
    if (!items || !items.length) continue;
    const block = element("div", null, "report-section");
    block.append(element("h4", title));
    const list = element("ul");
    for (const item of items) list.append(element("li", item));
    block.append(list);
    node.append(block);
  }
  const footer = element("div", null, "report-footer");
  const again = element("button", "读取已保存复盘 ↗", "text-button");
  again.type = "button";
  again.addEventListener("click", generateReport);
  footer.append(
    element("small", `来源：${report.source === "model" ? "Qwen 复盘" : "规则统计"} · ${formatDate(report.updated_at)}`),
    again,
  );
  node.append(footer);
}

async function generateReport() {
  const session = state.session;
  if (!session || session.status !== "completed" || state.reportBusy) return;
  const context = state.context;
  state.reportBusy = true;
  renderReportArea(session);
  try {
    const report = await request(`${base}/${session.id}/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: crypto.randomUUID() }),
      timeoutMs: 180000,
    });
    if (state.context === context) state.report = report;
  } catch (error) {
    if (state.context === context) report_error(error, session);
  } finally {
    if (state.context === context) {
      state.reportBusy = false;
      renderReportArea(state.session);
    }
  }
}

function report_error(error, session) {
  if (error instanceof ApiError && error.status === 0) {
    state.report = { status: "failed", error_code: "network", error_message: "连接中断，复盘可能仍在生成" };
    return;
  }
  notify(error.message || "复盘生成失败，请稍后重试。", true);
  if (session) state.report = { status: "failed", error_code: "failed", error_message: error.message || "" };
}

function canLeaveDraft() {
  return !$("answer-text").value.trim() || window.confirm("当前回答尚未提交，离开会丢弃这份草稿。继续吗？");
}

function resetSessionView() {
  state.context += 1;
  state.report = null;
  state.reportBusy = false;
  state.runtime = null;
  state.referencesBusy = false;
  closePanel(false);
  $("room-report").hidden = true;
  $("room-report").replaceChildren();
  $("message").hidden = true;
}

function openPanel(name, opener) {
  if (state.panel === name) { closePanel(); return; }
  if (!state.panel) {
    state.answerSelection = { start: $("answer-text").selectionStart, end: $("answer-text").selectionEnd };
  }
  state.returnFocus = opener;
  state.panel = name;
  const titles = { records: "面试记录", objectives: "考察目标", references: "参考答案与笔记", history: "历史问答", details: "场次详情" };
  $("assistance-title").textContent = titles[name];
  document.querySelectorAll(".drawer-panel").forEach((node) => { node.hidden = node.id !== `panel-${name}`; });
  document.querySelectorAll("[data-panel]").forEach((node) => { node.setAttribute("aria-expanded", String(node.dataset.panel === name)); });
  $("assistance").hidden = false;
  document.body.classList.add("assistance-open");
  $("assistance-title").focus();
  if (name === "references") {
    renderReferences();
    if (!state.runtime) loadReferences();
  }
}

function closePanel(restoreFocus = true) {
  $("assistance").hidden = true;
  document.body.classList.remove("assistance-open");
  document.querySelectorAll("[data-panel]").forEach((node) => node.setAttribute("aria-expanded", "false"));
  state.panel = null;
  if (restoreFocus && !$("answer-box").hidden && !$("interview-room").hidden && !state.busy) {
    $("answer-text").focus();
    if (state.answerSelection) $("answer-text").setSelectionRange(state.answerSelection.start, state.answerSelection.end);
  } else if (restoreFocus && state.returnFocus?.isConnected) {
    state.returnFocus.focus();
  }
  state.answerSelection = null;
  state.returnFocus = null;
}

function renderCurrentQuestion(session) {
  const box = $("current-question");
  box.replaceChildren();
  const turn = session.current_turn;
  if (!turn) {
    box.append(element("p", session.status === "completed" ? "本场面试已结束，接下来回顾你的回答。" : "正在准备下一步…", "question-waiting"));
    return;
  }
  const index = session.turns.filter((item) => item.role === "interviewer" && item.sequence <= turn.sequence).length;
  box.append(element("p", `${sourceLabels[turn.source] || "面试问题"} · 第 ${index} 问${turn.is_followup ? " · 追问" : ""}`, "question-kicker"));
  box.append(element("h3", turn.text, "question-title"));
  if (turn.objective_id) box.append(element("p", `当前考察：${objectiveText(session, turn.objective_id)}`, "question-context"));
}

async function loadReferences() {
  const session = state.session;
  if (!session || state.referencesBusy) return;
  const context = state.context;
  state.referencesBusy = true;
  $("retry-references").hidden = true;
  $("reference-content").replaceChildren(element("p", "正在读取本场计划资料…", "drawer-help"));
  try {
    const runtime = await request(`/interview-plans/${session.plan_id}/runtime`);
    if (state.context !== context) return;
    if (runtime.plan_revision !== session.plan_revision) throw new Error("资料版本与本场面试不一致，暂不展示参考答案。");
    state.runtime = runtime;
    renderReferences();
  } catch (error) {
    if (state.context !== context) return;
    $("reference-content").replaceChildren(element("p", error instanceof ApiError ? "无法读取本场计划资料。计划可能已被删除；面试与已保存问答仍可继续使用。" : error.message, "drawer-help"));
    $("retry-references").hidden = false;
  } finally {
    if (state.context === context) state.referencesBusy = false;
  }
}

function renderReferences() {
  const runtime = state.runtime;
  const session = state.session;
  if (!runtime || !session) return;
  const box = $("reference-content");
  box.replaceChildren();
  const objectiveId = session.current_turn?.objective_id;
  const objective = runtime.objectives.find((item) => item.id === objectiveId);
  if (!objective) {
    box.append(element("p", "有当前待回答问题时，这里会显示相关的冻结资料。", "drawer-help"));
    return;
  }
  box.append(element("h3", objective.topic));
  const ids = new Set(objective.question_ids);
  for (const question of runtime.materials.questions.filter((item) => ids.has(item.id))) {
    const article = element("article", null, "reference-item");
    article.append(element("h4", question.question_text), element("small", `题库版本 ${question.content_version}`));
    article.append(element("h5", "我的参考答案"), element("p", question.standard_answer_text || "未填写参考答案。", "reference-text"));
    if (question.user_notes) article.append(element("h5", "我的笔记"), element("p", question.user_notes, "reference-text"));
    box.append(article);
  }
  const factIds = new Set(objective.project_fact_ids);
  for (const project of runtime.materials.projects) {
    for (const fact of project.facts.filter((item) => factIds.has(item.id))) {
      const article = element("article", null, "reference-item");
      article.append(element("h4", project.name), element("p", fact.content, "reference-text"));
      box.append(article);
    }
  }
  if (box.children.length === 1) box.append(element("p", "当前目标没有可展示的参考资料。", "drawer-help"));
}

function bind() {
  document.querySelectorAll("[data-panel]").forEach((button) => {
    button.addEventListener("click", () => openPanel(button.dataset.panel, button));
  });
  $("close-assistance").addEventListener("click", () => closePanel());
  $("retry-references").addEventListener("click", loadReferences);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.panel) { event.preventDefault(); closePanel(); }
  });
  window.addEventListener("beforeunload", (event) => {
    if ($("answer-text").value.trim()) { event.preventDefault(); event.returnValue = ""; }
  });
  $("new-session").addEventListener("click", showStart);
  $("refresh-sessions").addEventListener("click", () => {
    loadSessions().then(() => notify("记录已刷新。")).catch(report);
  });
  $("session-previous").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - pageSize);
    loadSessions().catch(report);
  });
  $("session-next").addEventListener("click", () => {
    state.offset += pageSize;
    loadSessions().catch(report);
  });
  $("dismiss-message").addEventListener("click", () => { $("message").hidden = true; });
  $("start-form").addEventListener("submit", startSession);
  $("submit-answer").addEventListener("click", submitAnswer);
  $("answer-text").addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      submitAnswer();
    }
  });
  $("retry-step").addEventListener("click", retryStep);
  $("pause-session").addEventListener("click", () => control("pause"));
  $("resume-session").addEventListener("click", () => control("resume"));
  $("skip-objective").addEventListener("click", () => control(
    "skip", "跳过当前目标？未提交的草稿会清空，已提交的回答保留，之后不再追问这个目标。",
  ));
  $("end-session").addEventListener("click", () => control(
    "end", "结束这场面试？未提交的草稿不会保存，已保存的问答与目标进度都会保留。",
  ));
}

async function boot() {
  bind();
  try {
    await Promise.all([loadOptions(), loadSessions()]);
    $("connection").textContent = "已连接";
    $("connection").classList.add("ok");
    const unfinished = state.sessions.find((item) => item.status !== "completed");
    if (unfinished) await openSession(unfinished.id);
    else showWelcome();
  } catch (error) {
    $("connection").textContent = "读取失败";
    $("connection").classList.add("error");
    report(error);
    showWelcome();
  }
}

boot();
