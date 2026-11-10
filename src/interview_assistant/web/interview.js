import { initTabs, openOverlay } from "./components.js";

const $ = (id) => document.getElementById(id);
const base = "/interviews";
const pageSize = 10;
const state = {
  options: null, sessions: [], total: 0, offset: 0, sessionChecked: new Set(), deleteBusy: false,
  session: null, busy: false, timer: null, waitTimer: null, fetchedAt: 0,
  stream: null, live: false, report: null, reportBusy: false,
  context: 0, panel: null, returnFocus: null, runtime: null, referencesBusy: false,
  answerSelection: null, answerScroll: 0, panelRestore: true,
  phase: "", recovery: false, answerRequest: null, creation: null, reportRequest: null,
  reportReading: false, reportLoadError: "", pollVersion: 0,
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
const formatClock = (value) => {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
};
const wideWorkspace = window.matchMedia("(min-width: 1280px)");

function sessionName(session) {
  const date = new Date(session.started_at).toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
  return `${session.direction || "模拟面试"} · ${date}`;
}

function resizeAnswer() {
  const node = $("answer-text");
  $("answer-count").textContent = `${node.value.length} / 8000`;
  if (node.offsetParent === null) return;
  node.style.height = "auto";
  node.style.height = `${Math.min(600, node.scrollHeight + 2)}px`;
}

function focusAnswer() {
  if (!state.panel && !$("answer-box").hidden && !$("answer-text").disabled) {
    $("answer-text").focus({ preventScroll: true });
  }
}

function renderStage() {
  const session = state.session;
  if (!session) return;
  let label;
  if (session.status === "paused") label = "已暂停 · 回答草稿仍保留在本页";
  else if (session.status === "completed") label = "本场已结束";
  else if (state.phase === "submitting") label = "提交中 · 正在确认回答是否保存";
  else if (state.phase === "streaming") label = "面试官正在回应 · 等待正式问题";
  else if (state.recovery) label = "连接恢复中 · 正在核对本场状态";
  else if (state.live || state.stream || state.phase === "waiting") label = "回答已保存 · 正在生成下一步";
  else if (state.busy) label = "正在更新本场状态";
  else if (!session.current_turn) label = session.error_code ? "生成未完成 · 可以继续" : "等待下一问";
  else label = session.current_turn.is_followup ? "追问已就绪 · 请继续回答" : "问题已就绪 · 正在回答";
  $("stage-label").textContent = label;
  $("stage-label").parentElement.dataset.waiting = String(state.busy || state.live || Boolean(state.stream) || state.recovery);
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
  // Read-only assistance remains available during a request. Only mutations lock.
  $("welcome-start").disabled = value;
  $("review-new").disabled = value;
  $("plan-select").disabled = value || !planOptions().some((item) => item.usable);
  $("session-mode").disabled = value;
  $("start-session").disabled = value || !selectedPlanUsable();
  renderSessionList();
  if (state.session) { renderControls(state.session); renderStage(); }
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
    $("stage-detail").textContent = `已等待 ${Math.floor((Date.now() - started) / 1000)} 秒`;
  };
  tick();
  state.waitTimer = setInterval(tick, 200);
}

function stopWait() {
  if (state.waitTimer) clearInterval(state.waitTimer);
  state.waitTimer = null;
  $("stage-detail").textContent = "";
  $("answer-hint").textContent = "未提交的回答仅保留在本页。";
  $("room-note").textContent = "提交后先保存回答，再生成下一步。";
}

// 回答提交后由服务端后台决策，这里订阅进度；拿到可作答状态就关流。
function closeStream() {
  state.pollVersion += 1;
  if (state.stream) state.stream.close();
  state.stream = null;
  if (state.session) renderControls(state.session);
}

function subscribe(id) {
  closeStream();
  const context = state.context;
  const stream = new EventSource(`${base}/${id}/events`);
  state.stream = stream;
  stream.addEventListener("state", async (event) => {
    let updated;
    try { updated = JSON.parse(event.data); } catch { return; }
    if (state.context !== context || !state.session || state.session.id !== id) return;
    state.session = updated;
    state.fetchedAt = Date.now();
    renderSession(updated);
    const settled = updated.status !== "active"
      || Boolean(updated.current_turn) || Boolean(updated.error_code);
    if (!settled) return;
    state.recovery = false;
    closeStream();
    stopWait();
    loadSessions().catch(report);
    renderStage();
    focusAnswer();
    if (updated.error_code) {
      notify(`下一步没有生成成功：${updated.error_message || updated.error_code}。可点「重试下一步」。`, true);
    }
  });
  stream.addEventListener("error", () => {
    // 服务端正常关闭也会触发 error；只有仍挂在流上时才需要兜底。
    if (state.stream !== stream) return;
    closeStream();
    state.recovery = true;
    if (state.session) renderControls(state.session);
    renderStage();
    pollUntilSettled(id);
  });
  if (state.session) renderControls(state.session);
}

// SSE 不可用（代理缓冲、断线）时退化为轮询，避免用户一直停在等待态。
async function pollUntilSettled(id) {
  const context = state.context;
  const pollVersion = state.pollVersion;
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    if (state.context !== context || state.pollVersion !== pollVersion || !state.session || state.session.id !== id) return;
    try {
      const updated = await request(`${base}/${id}`);
      if (state.context !== context || state.pollVersion !== pollVersion) return;
      if (updated.status !== "active" || updated.current_turn || updated.error_code) {
        state.session = updated;
        state.recovery = false;
        state.fetchedAt = Date.now();
        stopWait();
        renderSession(updated);
        loadSessions().catch(report);
        focusAnswer();
        return;
      }
    } catch { break; }
  }
  if (state.context !== context || state.pollVersion !== pollVersion) return;
  state.recovery = false;
  stopWait();
  renderControls(state.session);
  renderStage();
  notify("暂时未能确认生成结果，请先「重新读取状态」；确认生成失败后可继续生成。", true);
}

function showWelcome() {
  document.body.classList.remove("in-session", "review-mode");
  $("entry-heading").hidden = false;
  $("review-panel").hidden = true;
  $("interview-welcome").hidden = false;
  $("start-panel").hidden = true;
  $("interview-room").hidden = true;
  const note = $("welcome-note");
  if (!state.options) return;
  note.textContent = planOptions().some((item) => item.usable)
    ? "点击右上角「新建面试」选择计划开始；回答提交后先落库，面试官的话随即逐字出现。"
    : planOptions().length ? "现有计划缺少当前面试所需的运行依据，请前往面试计划重新生成并确认。" : "还没有可用的已确认计划。先前往面试计划，再回来开始第一场练习。";
}

function showStart() {
  if (state.busy || !canLeaveDraft()) return;
  resetSessionView();
  closeStream();
  stopWait();
  state.session = null;
  state.creation = null;
  clearInterval(state.timer);
  $("answer-text").value = "";
  $("interview-welcome").hidden = true;
  $("interview-room").hidden = true;
  $("start-panel").hidden = false;
  $("review-panel").hidden = true;
  $("entry-heading").hidden = false;
  document.body.classList.remove("in-session", "review-mode");
  renderPlanOptions();
  $("plan-select").focus();
}

function showRoom() {
  $("interview-welcome").hidden = true;
  $("start-panel").hidden = true;
  const completed = state.session?.status === "completed";
  $("entry-heading").hidden = true;
  $("interview-room").hidden = completed;
  $("review-panel").hidden = !completed;
  document.body.classList.toggle("in-session", !completed);
  document.body.classList.toggle("review-mode", completed);
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
  renderPlanPreview();
}

function renderPlanPreview() {
  const plan = selectedPlan();
  const node = $("plan-preview");
  node.replaceChildren();
  node.hidden = !plan?.usable;
  if (!plan?.usable) return;
  for (const [label, value] of [["练习方向", plan.direction || "未填写"], ["考察目标", `${plan.objective_count} 个`], ["预计时长", `${plan.duration_minutes} 分钟`]]) {
    const entry = element("div");
    entry.append(element("dt", label), element("dd", value));
    node.append(entry);
  }
  $("start-session").disabled = state.busy || !selectedPlanUsable();
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
    const card = element("div", null, "session-card");
    card.classList.toggle("selected", state.session?.id === session.id);
    const check = document.createElement("input");
    check.type = "checkbox";
    check.className = "session-check";
    check.checked = state.sessionChecked.has(session.id);
    check.disabled = state.busy || state.reportBusy || state.deleteBusy || session.status === "active";
    if (session.status === "active") check.title = "进行中的场次需先结束或暂停，才能删除";
    check.setAttribute("aria-label", `选择记录：${sessionName(session)}`);
    check.addEventListener("change", () => {
      if (check.checked) state.sessionChecked.add(session.id);
      else state.sessionChecked.delete(session.id);
      renderSessionBatchControls();
    });
    const main = element("button", null, "session-main");
    main.type = "button";
    main.disabled = state.busy || state.reportBusy;
    const title = element("strong", session.direction || "模拟面试");
    const line = element("small",
      `${statusLabels[session.status] || session.status} · ${session.mode === "model" ? "Qwen" : "规则"} · `
      + `${session.answer_count} 轮回答 · 目标 ${session.objective_done}/${session.objective_total}`);
    const time = element("small", `${formatDate(session.created_at)} · 用时 ${formatSeconds(session.elapsed_sec)}`);
    main.append(title, line, time);
    main.addEventListener("click", () => { openSession(session.id); });
    card.append(check, main);
    list.append(card);
  }
  renderSessionBatchControls();
  $("session-page").textContent = `第 ${Math.floor(state.offset / pageSize) + 1} 页`;
  $("session-previous").disabled = state.busy || state.offset === 0;
  $("session-next").disabled = state.busy || state.offset + pageSize >= state.total;
}

function renderSessionBatchControls() {
  const button = $("delete-sessions");
  // 只要有可删记录（非进行中）就显示入口；是否可点取决于勾选数。
  button.hidden = !state.sessions.some((item) => item.status !== "active");
  button.disabled = state.busy || state.reportBusy || state.deleteBusy || !state.sessionChecked.size;
  button.textContent = state.sessionChecked.size ? `批量删除（${state.sessionChecked.size}）` : "批量删除";
}

async function deleteSessions() {
  if (state.deleteBusy || state.busy || state.reportBusy || !state.sessionChecked.size) return;
  const ids = [...state.sessionChecked];
  const active = state.sessions.filter((item) => ids.includes(item.id) && item.status === "active");
  if (active.length) {
    notify(`有 ${active.length} 场面试仍在进行中，需先结束或暂停才能删除。`, true);
    return;
  }
  const confirmed = window.confirm(
    `永久删除选中的 ${ids.length} 场面试记录？\n\n`
    + "全部问答、目标进度与整场复盘都会一起删除，删除后不可恢复；已确认的面试计划不受影响。",
  );
  if (!confirmed) return;
  state.deleteBusy = true;
  renderSessionBatchControls();
  try {
    const result = await request(`${base}/batch-delete`, {
      method: "POST",
      body: JSON.stringify({ session_ids: ids }),
      timeoutMs: 30000,
    });
    for (const id of ids) state.sessionChecked.delete(id);
    const remaining = Math.max(0, state.total - (result.deleted ?? ids.length));
    if (state.offset > 0 && state.offset >= remaining) {
      state.offset = Math.floor(Math.max(0, remaining - 1) / pageSize) * pageSize;
    }
    if (state.session && ids.includes(state.session.id)) {
      // 当前打开的场次也被删除：关掉视图回到欢迎页。
      closeStream();
      stopWait();
      resetSessionView();
      state.session = null;
      showWelcome();
    }
    await loadSessions();
    notify(`已删除 ${result.deleted ?? ids.length} 场面试记录。`);
  } catch (error) {
    report(error);
  } finally {
    state.deleteBusy = false;
    renderSessionBatchControls();
  }
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
    $("answer-text").value = "";
    showRoom();
    renderSession(session);
    (session.status === "completed" ? $("review-title") : $("room-title")).focus();
    renderSessionList();
    startTimer();
    // 已结束的场次顺带读一次复盘；没有生成过（404）就保持无报告状态。
    if (session.status === "completed") {
      readSavedReport();
    }
    // 刷新时若上一轮决策还在后台跑，直接续订进度，而不是让用户点重试。
    if (session.status === "active" && session.pending_turn_id && !session.current_turn && !session.error_code) {
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
  const mode = $("session-mode").value;
  if (state.creation?.plan_id !== planId || state.creation?.mode !== mode) {
    state.creation = { request_id: crypto.randomUUID(), plan_id: planId, mode };
  }
  setBusy(true);
  // 开场只为一个目标生成基础问题，等待与单轮出题同量级。
  $("connection").textContent = "正在生成首问";
  $("start-session").textContent = "正在生成首问…";
  $("start-status").textContent = "正在创建场次并准备首问，请稍候。";
  try {
    const session = await request(base, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.creation),
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
    $("start-status").textContent = "未能确认结果。保持计划与模式不变重试，会读取同一场次。";
    loadSessions().catch(() => {});
  } finally {
    $("start-session").textContent = "开始面试";
    setBusy(false);
  }
}

async function submitAnswer() {
  const session = state.session;
  if (!session || session.status !== "active" || !session.current_turn || state.busy || state.stream || state.recovery || state.live) return;
  const text = $("answer-text").value.trim();
  if (!text) {
    notify("先写下你的回答再提交。", true);
    $("answer-text").focus();
    return;
  }
  closeStream();
  if (state.answerRequest?.turn_id !== session.current_turn.id || state.answerRequest?.answer_text !== text) {
    state.answerRequest = { request_id: crypto.randomUUID(), turn_id: session.current_turn.id, answer_text: text };
  }
  state.live = true;
  state.phase = "submitting";
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
        state.phase = "waiting";
        $("answer-text").value = "";
        renderSession(read);
        bubble = beginBubble();
        startWait("回答已保存，正在生成下一步…");
      },
      delta(payload) {
        state.phase = "streaming";
        renderStage();
        if (!bubble) {
          bubble = beginBubble();
          // 第一个字到了就不必再显示等待秒数。
          stopWait();
        }
        bubble.textNode.textContent += payload.text || "";
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
        // An error frame is not a final session snapshot. Read state before retrying.
        notify(`回答已保存，但${payload.message || "下一步没有生成成功"}。可点「重试下一步」。`, true);
      },
    });
  } catch (error) {
    // 流建立失败时回答可能已经保存：回读一次再提示，避免用户重复提交。
    report(error);
  } finally {
    if (bubble) bubble.card.remove();
    state.live = false;
    state.phase = "";
    stopWait();
    if (!settled) await refreshQuietly();
    setBusy(false);
    if (state.session?.current_turn) focusAnswer();
  }
}

// 提交回答并逐帧读取：ack（已保存）、delta（面试官的话）、state（最终快照）、error。
async function readAnswerStream(session, text, handlers) {
  let response;
  try {
    response = await fetch(`${base}/${session.id}/answers/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(state.answerRequest),
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
  card.setAttribute("aria-label", "临时生成内容，最终问题以保存结果为准");
  return { card, textNode };
}

async function retryStep() {
  const session = state.session;
  if (!session || state.busy || state.stream || state.live || state.recovery) return;
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
    focusAnswer();
  }
}

async function control(action, confirmText) {
  const session = state.session;
  if (!session || state.busy || state.live || state.stream || state.recovery) return;
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
    if (action === "skip" || action === "end") $("answer-text").value = "";
    state.session = updated;
    state.fetchedAt = Date.now();
    renderSession(updated);
    if (updated.status === "completed") $("review-title").focus();
    await loadSessions();
    if (updated.error_code) notify(`${updated.error_message || updated.error_code}`, true);
  } catch (error) {
    report(error);
    await refreshQuietly();
  } finally {
    setBusy(false);
    if (action === "resume") focusAnswer();
  }
}

async function refreshQuietly() {
  const session = state.session;
  const context = state.context;
  if (!session) return false;
  try {
    const updated = await request(`${base}/${session.id}`);
    if (state.context !== context || state.session?.id !== session.id) return false;
    // A changed turn must not receive the previous question's draft. Keep a copy.
    if (updated.answer_count > session.answer_count
        || updated.current_turn?.id !== session.current_turn?.id) {
      preserveDraft();
      $("answer-text").value = "";
    }
    state.session = updated;
    state.fetchedAt = Date.now();
    renderSession(updated);
    loadSessions().catch(report);
    if (updated.status === "active" && updated.pending_turn_id && !updated.current_turn && !updated.error_code && !state.live) {
      startWait("回答已保存，正在恢复本场状态…");
      subscribe(updated.id);
    }
    return true;
  } catch { return false; }
}

function preserveDraft() {
  const text = $("answer-text").value;
  if (!text.trim()) return;
  const previous = $("preserved-answer").textContent;
  if (previous !== text) $("preserved-answer").textContent = previous ? `${previous}\n\n—— 另一份保留文本 ——\n\n${text}` : text;
  $("preserved-draft").hidden = false;
}

function startTimer() {
  clearInterval(state.timer);
  state.timer = setInterval(() => {
    const session = state.session;
    const node = $("room-elapsed");
    if (!session || !node) return;
    // Server elapsed time is wall time, including pauses; the UI uses the same basis.
    const extra = session.status !== "completed" ? Math.floor((Date.now() - state.fetchedAt) / 1000) : 0;
    const strong = node.querySelector("strong");
    if (strong) strong.textContent = formatClock(session.elapsed_sec + extra);
  }, 1000);
}

function objectiveText(session, objectiveId) {
  const objective = session.objectives.find((item) => item.id === objectiveId);
  if (!objective) return objectiveId || "未关联目标";
  return `${objective.id} · ${objective.topic}`;
}

function renderSession(session) {
  const enteringReview = session.status === "completed" && $("review-panel").hidden;
  if (enteringReview) {
    closePanel(false);
    preserveDraft();
    $("report-tab").click();
  }
  showRoom();
  $("room-status").textContent = statusLabels[session.status] || session.status;
  $("room-status").className = session.status === "paused" ? "ds-badge ds-badge--warning" : "ds-badge";
  $("room-title").textContent = sessionName(session);
  $("review-subtitle").textContent = sessionName(session);
  if (session.status === "completed") $("answer-text").value = "";
  renderMeta(session);
  renderObjectives(session);
  renderCurrentQuestion(session);
  renderFeedback(session);
  renderConversation(session);
  if (session.status === "completed") renderConversation(session, $("review-conversation"));
  if (state.panel === "references") renderReferences();
  if (state.panel === "knowledge") renderKnowledge();
  renderControls(session);
  renderNotice(session);
  renderSummary(session);
  renderReportArea(session);
  renderStage();
  resizeAnswer();
}

function renderFeedback(session) {
  const latest = [...session.turns].reverse().find((turn) => turn.role === "candidate");
  const node = $("feedback-content");
  node.replaceChildren();
  $("last-feedback").hidden = !latest;
  if (!latest) return;
  if (node.dataset.turn !== String(latest.id)) $("last-feedback").open = false;
  node.dataset.turn = String(latest.id);
  node.append(element("p", "上一次已保存的回答", "feedback-label"), element("p", latest.text));
  if (latest.decision?.observation) {
    node.append(element("p", session.mode === "model" ? "面试官观察 · 供复盘参考" : "规则模式观察", "feedback-label"), element("p", latest.decision.observation));
  }
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
    ["冻结计划", `${session.plan_id} · revision ${session.plan_revision}`],
    ["资料说明", "参考资料仅从本场计划固定版本读取，不跟随题库和简历更新。"],
    ["计时口径", "本场总经过时间，包含暂停时段。"],
  ];
  for (const [label, value] of entries) {
    const span = element("span");
    span.append(element("small", `${label} `), element("strong", value));
    node.append(span);
  }
  $("room-elapsed").querySelector("strong").textContent = formatClock(session.elapsed_sec);
  $("target-duration").textContent = `预计 ${Math.round(session.duration_target_sec / 60)} 分钟`;
  const skipped = session.objectives.filter((item) => item.status === "skipped").length;
  $("workspace-progress").textContent = `已提问 ${session.question_count} 次 · 目标覆盖 ${covered}/${session.objectives.length}${skipped ? ` · 跳过 ${skipped}` : ""}`;
  const remaining = session.duration_target_sec - session.elapsed_sec;
  node.append(element("span", remaining >= 0
    ? `计划剩余 ${formatSeconds(remaining)}`
    : `已超计划 ${formatSeconds(-remaining)}`));
}

function renderObjectives(session) {
  const rail = $("objective-rail");
  rail.replaceChildren();
  for (const objective of session.objectives) {
    const chip = element("article", `${objective.id} · ${objective.topic}`, `rail-chip ${objective.status}`);
    chip.title = `${sourceLabels[objective.source]} · ${priorityLabels[objective.priority]}优先级 · `
      + `${objectiveLabels[objective.status]} · 已问 ${objective.asked} · 追问 ${objective.followups}/${objective.max_followups}`;
    chip.append(element("small", `${objectiveLabels[objective.status]} · 已问 ${objective.asked} · 追问 ${objective.followups}/${objective.max_followups}`));
    const saved = state.runtime?.objectives.find((item) => item.id === objective.id);
    if (saved) {
      const evidence = element("ul", null, "objective-evidence");
      for (const item of saved.observable_evidence) evidence.append(element("li", item));
      if (evidence.children.length) chip.append(evidence);
      if (saved.completion_rule) chip.append(element("small", `完成条件：${saved.completion_rule}`));
    }
    rail.append(chip);
  }
}

function renderConversation(session, box = $("conversation")) {
  const key = `${session.id}:${session.revision}`;
  if (box.dataset.version === key) return;
  box.dataset.version = key;
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
    card.id = `${box.id}-turn-${turn.sequence}`;
    card.tabIndex = -1;
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
}

function renderControls(session) {
  const active = session.status === "active";
  const paused = session.status === "paused";
  const completed = session.status === "completed";
  const hasQuestion = Boolean(session.current_turn);
  const streaming = Boolean(state.stream) || state.live;
  const locked = state.busy || streaming || state.recovery;
  $("answer-box").hidden = !(active && hasQuestion);
  $("skip-objective").hidden = !(active && hasQuestion);
  $("pause-session").hidden = !active;
  $("resume-session").hidden = !paused;
  $("retry-step").hidden = !(active && !hasQuestion);
  $("retry-step").textContent = streaming
    ? "正在生成下一步…"
    : (session.pending_turn_id ? "重试下一步" : "继续生成下一问");
  $("retry-step").disabled = locked;
  $("end-session").hidden = completed;
  $("submit-answer").disabled = locked || !active || !hasQuestion;
  $("answer-text").disabled = locked || !active || !hasQuestion;
  $("pause-session").disabled = locked;
  $("resume-session").disabled = locked;
  $("skip-objective").disabled = locked;
  $("end-session").disabled = locked;
  $("refresh-current").disabled = state.busy || state.live;
  $("room-note").textContent = completed
    ? "本场记录已保存，可回看每一轮问答。"
    : streaming ? "下一步正在生成，暂停和结束暂不可用；仍可展开辅助资料。" : "提交后先保存回答，再生成下一步。";
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
    notice.textContent = "已暂停。当前问题与已提交回答已保存；未提交的草稿保留在本页，刷新会丢失。点击「继续面试」接着答。";
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
    const loading = element("p", state.reportReading ? "正在读取已保存的复盘…" : "正在生成整场复盘：问答记录已保存，完成后会显示逐目标观察与练习建议。", "report-loading");
    loading.setAttribute("role", "status");
    node.append(loading);
    return;
  }
  if (state.reportLoadError && !report) {
    node.append(element("h3", "暂时无法读取复盘"), element("p", state.reportLoadError, "report-failed"));
    const retry = element("button", "重新读取已保存复盘", "ds-button");
    retry.type = "button";
    retry.addEventListener("click", readSavedReport);
    node.append(retry);
    return;
  }
  if (report && report.status === "ready" && report.report) {
    renderReport(node, report, session);
    return;
  }
  if (report && report.status !== "ready") {
    node.append(element("h3", "复盘没有生成成功"));
    node.append(element("p", `${report.error_message || report.error_code || "生成失败"}。问答记录已保存，可以直接重试。`, "report-failed"));
    const retry = element("button", "重试生成复盘", "ds-button");
    retry.type = "button";
    retry.addEventListener("click", generateReport);
    node.append(retry);
    return;
  }
  const intro = element("p", session.mode === "rule" ? "根据本场已保存记录生成规则统计，回看目标进度与回答，不调用模型。" : "让 Qwen 复盘整场问答：逐目标观察、弱项与下次练习建议。题库题参考本场冻结版本的个人标准答案。", "report-intro");
  const start = element("button", "生成整场复盘", "ds-button ds-button--primary");
  start.type = "button";
  start.addEventListener("click", generateReport);
  node.append(element("h3", "整场复盘"), intro, start);
}

function renderReport(node, report, session) {
  const content = report.report;
  const head = element("div", null, "report-head");
  const badge = element("span", overallLabels[content.overall] || content.overall, `report-badge ${content.overall}`);
  head.append(element("h3", "整场复盘"), badge);
  node.append(head, element("p", "以下为模型观察或规则统计，结合原始回答核对，不代表确定的能力评级。", "ds-caption"));
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
        const evidence = element("div", null, "report-evidence");
        evidence.append(element("small", "关联的已保存回答"));
        for (const sequence of item.turn_sequences) {
          const turn = session.turns.find((entry) => entry.role === "candidate" && entry.sequence === sequence);
          if (!turn) continue;
          const index = session.turns.filter((entry) => entry.role === "candidate" && entry.sequence <= sequence).length;
          const button = element("button", `第 ${index} 轮回答 ↗`, "text-button");
          button.type = "button";
          button.addEventListener("click", () => {
            $("transcript-tab").click();
            const target = $(`review-conversation-turn-${sequence}`);
            target?.scrollIntoView({ block: "start" });
            target?.focus({ preventScroll: true });
          });
          evidence.append(button);
        }
        if (evidence.children.length > 1) card.append(evidence);
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
  again.addEventListener("click", readSavedReport);
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
  state.reportReading = false;
  state.reportLoadError = "";
  state.reportRequest ||= crypto.randomUUID();
  renderSessionList();
  renderReportArea(session);
  try {
    const report = await request(`${base}/${session.id}/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: state.reportRequest }),
      timeoutMs: 180000,
    });
    if (state.context === context) state.report = report;
  } catch (error) {
    if (state.context === context) report_error(error, session);
  } finally {
    if (state.context === context) {
      state.reportBusy = false;
      // A known failed generation can be explicitly retried as a new request.
      if (state.report?.status === "failed" && state.report.error_code !== "network") state.reportRequest = null;
      renderReportArea(state.session);
      renderSessionList();
    }
  }
}

async function readSavedReport() {
  const session = state.session;
  const context = state.context;
  if (!session || state.reportBusy) return;
  state.reportBusy = true;
  state.reportReading = true;
  state.reportLoadError = "";
  renderSessionList();
  renderReportArea(session);
  try {
    const saved = await request(`${base}/${session.id}/report`);
    if (state.context === context) state.report = saved;
  } catch (error) {
    if (state.context === context) {
      if (error instanceof ApiError && error.status === 404) state.report = null;
      else { state.reportLoadError = "问答记录仍可回看。请重试读取，不会重新调用模型。"; report(error); }
    }
  }
  finally {
    if (state.context === context) {
      state.reportBusy = false;
      state.reportReading = false;
      renderReportArea(state.session);
      renderSessionList();
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
  return (!$("answer-text").value.trim() && $("preserved-draft").hidden)
    || window.confirm("当前有尚未提交或保留的回答文字，离开会丢弃这些本页草稿。继续吗？");
}

function resetSessionView() {
  state.context += 1;
  state.report = null;
  state.reportBusy = false;
  state.reportRequest = null;
  state.reportReading = false;
  state.reportLoadError = "";
  state.answerRequest = null;
  state.recovery = false;
  state.phase = "";
  state.runtime = null;
  state.referencesBusy = false;
  closePanel(false);
  $("room-report").hidden = true;
  $("room-report").replaceChildren();
  $("message").hidden = true;
  $("preserved-draft").hidden = true;
  $("preserved-answer").textContent = "";
  $("last-feedback").open = false;
  $("report-tab").click();
}

function openPanel(name, opener) {
  if (name !== "records" && !state.session) return;
  if (state.panel === name) { closePanel(); return; }
  if (!state.panel) {
    state.answerSelection = { start: $("answer-text").selectionStart, end: $("answer-text").selectionEnd };
    state.answerScroll = $("answer-text").scrollTop;
  }
  if (!$("assistance").contains(opener)) state.returnFocus = opener;
  state.panel = name;
  const titles = { records: "面试记录", objectives: "面试目标", references: "个人参考答案", knowledge: "相关知识点", history: "历史对话", details: "本场冻结资料" };
  $("assistance-title").textContent = titles[name];
  document.querySelectorAll(".drawer-panel").forEach((node) => { node.hidden = node.id !== `panel-${name}`; });
  document.querySelectorAll("[data-panel]").forEach((node) => { node.setAttribute("aria-expanded", String(node.dataset.panel === name)); });
  $("assistance").hidden = false;
  document.body.classList.add("assistance-open");
  $("assistance").querySelector(".panel-switcher").hidden = !state.session;
  state.panelRestore = true;
  if (!$("assistance").open) {
    if (wideWorkspace.matches) $("assistance").show();
    else openOverlay($("assistance"), { returnFocus: null });
  }
  $("assistance-title").focus();
  if (name === "references") {
    renderReferences();
    if (!state.runtime) loadReferences();
  }
  if (name === "knowledge") {
    renderKnowledge();
    if (!state.runtime) loadReferences();
  }
  if (name === "objectives" && !state.runtime) loadReferences();
}

function closePanel(restoreFocus = true) {
  state.panelRestore = restoreFocus;
  if ($("assistance").open) $("assistance").close();
  else finishPanelClose();
}

function finishPanelClose() {
  const restoreFocus = state.panelRestore;
  $("assistance").hidden = true;
  document.body.classList.remove("assistance-open");
  document.querySelectorAll("[data-panel]").forEach((node) => node.setAttribute("aria-expanded", "false"));
  state.panel = null;
  if (restoreFocus && !$("answer-box").hidden && !$("interview-room").hidden && !$("answer-text").disabled) {
    $("answer-text").focus({ preventScroll: true });
    if (state.answerSelection) $("answer-text").setSelectionRange(state.answerSelection.start, state.answerSelection.end);
    $("answer-text").scrollTop = state.answerScroll;
  } else if (restoreFocus && state.returnFocus?.isConnected) {
    state.returnFocus.focus({ preventScroll: true });
  }
  state.answerSelection = null;
  state.returnFocus = null;
}

function renderCurrentQuestion(session) {
  const box = $("current-question");
  box.replaceChildren();
  const turn = session.current_turn;
  if (!turn) {
    box.append(element("p", session.error_code ? "下一步尚未就绪" : session.status === "paused" ? "已暂停，恢复后继续" : "正在准备下一步…", "question-waiting"));
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
  $("retry-knowledge").hidden = true;
  $("reference-content").replaceChildren(element("p", "正在读取本场计划资料…", "drawer-help"));
  $("knowledge-content").replaceChildren(element("p", "正在读取本场冻结资料…", "drawer-help"));
  try {
    const runtime = await request(`/interview-plans/${session.plan_id}/runtime`);
    if (state.context !== context) return;
    if (runtime.plan_id !== session.plan_id || runtime.plan_revision !== session.plan_revision) throw new Error("资料版本与本场面试不一致，暂不展示参考答案。");
    state.runtime = runtime;
    renderReferences();
    renderKnowledge();
    renderObjectives(state.session);
  } catch (error) {
    if (state.context !== context) return;
    $("reference-content").replaceChildren(element("p", error instanceof ApiError ? "无法读取本场计划资料。计划可能已被删除；面试与已保存问答仍可继续使用。" : error.message, "drawer-help"));
    $("retry-references").hidden = false;
    $("knowledge-content").replaceChildren(element("p", "无法读取本场固定版本的知识线索；不会使用最新资料替代。", "drawer-help"));
    $("retry-knowledge").hidden = false;
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

function renderKnowledge() {
  const runtime = state.runtime;
  const session = state.session;
  if (!runtime || !session) return;
  const box = $("knowledge-content");
  box.replaceChildren();
  const objective = runtime.objectives.find((item) => item.id === session.current_turn?.objective_id);
  if (!objective) {
    box.append(element("p", "当前没有待回答的问题，暂无相关知识线索。", "drawer-help"));
    return;
  }
  box.append(element("h3", objective.topic));
  const facts = new Set(objective.project_fact_ids);
  for (const project of runtime.materials.projects) {
    const related = project.facts.filter((fact) => facts.has(fact.id));
    if (!related.length) continue;
    const article = element("article", null, "knowledge-item");
    article.append(element("h4", project.name));
    if (project.tech_stack.length) article.append(element("p", `资料中的技术栈：${project.tech_stack.join("、")}`));
    for (const fact of related) article.append(element("p", fact.content));
    box.append(article);
  }
  const questions = new Set(objective.question_ids);
  for (const question of runtime.materials.questions.filter((item) => questions.has(item.id) && item.user_notes)) {
    const article = element("article", null, "knowledge-item");
    article.append(element("h4", `个人笔记 · ${question.question_text}`), element("p", question.user_notes));
    box.append(article);
  }
  if (box.children.length === 1) box.append(element("p", "当前目标只有主题，没有其他已保存的知识线索。可在参考答案面板阅读个人原文。", "drawer-help"));
}

function bind() {
  initTabs($("review-tabs"));
  document.querySelectorAll("[data-panel]").forEach((button) => {
    button.addEventListener("click", () => openPanel(button.dataset.panel, button));
  });
  $("close-assistance").addEventListener("click", () => closePanel());
  $("assistance").addEventListener("close", finishPanelClose);
  wideWorkspace.addEventListener("change", () => { if (state.panel) closePanel(); });
  $("retry-references").addEventListener("click", loadReferences);
  $("retry-knowledge").addEventListener("click", loadReferences);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.panel) { event.preventDefault(); closePanel(); }
  });
  window.addEventListener("beforeunload", (event) => {
    if (state.busy || state.reportBusy || $("answer-text").value.trim() || !$("preserved-draft").hidden) { event.preventDefault(); event.returnValue = ""; }
  });
  $("new-session").addEventListener("click", showStart);
  $("welcome-start").addEventListener("click", showStart);
  $("review-new").addEventListener("click", showStart);
  $("plan-select").addEventListener("change", renderPlanPreview);
  $("refresh-sessions").addEventListener("click", () => {
    loadSessions().then(() => notify("记录已刷新。")).catch(report);
  });
  $("delete-sessions").addEventListener("click", () => { deleteSessions().catch(report); });
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
  const rememberSelection = () => {
    if (!state.panel || document.activeElement !== $("answer-text")) return;
    state.answerSelection = { start: $("answer-text").selectionStart, end: $("answer-text").selectionEnd };
    state.answerScroll = $("answer-text").scrollTop;
  };
  $("answer-text").addEventListener("input", () => { resizeAnswer(); rememberSelection(); });
  $("answer-text").addEventListener("select", rememberSelection);
  $("answer-text").addEventListener("scroll", rememberSelection);
  $("dismiss-preserved").addEventListener("click", () => {
    if (!window.confirm("清除这份本页保留文本？已保存的面试记录不会改变。")) return;
    $("preserved-answer").textContent = "";
    $("preserved-draft").hidden = true;
    (state.session?.status === "completed" ? $("review-title") : $("room-title")).focus();
  });
  $("refresh-current").addEventListener("click", async () => {
    if (state.busy || state.live) return;
    closeStream();
    state.recovery = false;
    setBusy(true);
    const read = await refreshQuietly();
    setBusy(false);
    notify(read ? "已读取本场最新状态。" : "读取失败，当前文字已保留，请稍后重试。", !read);
  });
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
  for (const link of document.querySelectorAll(".app-navigation a, .app-brand, .welcome a")) {
    link.addEventListener("click", (event) => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      if (state.busy || !canLeaveDraft()) event.preventDefault();
    });
  }
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
