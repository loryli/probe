export function setupAiProjects({ request, run, getResume, isResumeDirty, notify, onLoadCandidate, progress = (_label, action) => action() }) {
  const $ = (id) => document.getElementById(id);
  const state = { resumeId: null, items: [], selectedId: null, pending: null, offset: 0, total: 0 };
  const labels = { running: "提取中", ready: "待核对", confirmed: "已全部保存", failed: "提取失败" };
  const text = (tag, value, className) => {
    const node = document.createElement(tag);
    node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  const path = () => `/materials/resumes/${state.resumeId}/parse-drafts`;
  const current = () => state.items.find((item) => item.id === state.selectedId);
  const listen = (id, event, action) => $(id).addEventListener(event, () => void run(action));

  async function modelStatus() {
    try {
      const status = await request("/ai/status");
      $("ai-model-status").textContent = status.configured ? `Qwen 已配置 · ${status.model}` :
        `尚未配置：请在本地 .env 填写 ${status.missing.join("、")}，再重启服务`;
    } catch {
      $("ai-model-status").textContent = "暂时无法读取模型配置，请确认服务已更新。";
    }
  }

  function render() {
    const draft = current();
    const selector = $("ai-draft-select");
    selector.replaceChildren();
    for (const item of state.items) {
      const option = text("option", `${new Date(item.created_at).toLocaleString("zh-CN", { hour12: false })} · v${item.source_version} · ${labels[item.status] || item.status}`);
      option.value = item.id;
      selector.append(option);
    }
    selector.value = state.selectedId || "";
    selector.disabled = !state.items.length;
    $("ai-draft-previous").disabled = state.offset === 0;
    $("ai-draft-next").disabled = state.offset + 10 >= state.total;
    $("ai-draft-page").textContent = `${Math.floor(state.offset / 10) + 1} / ${Math.max(1, Math.ceil(state.total / 10))}`;
    $("ai-extract-projects").textContent = state.pending || draft?.status === "running" ? "读取本次结果" : "提取候选项目";
    $("ai-extract-projects").title = getResume() ? `仅发送已保存简历 v${getResume().content_version} 的文字，不上传 PDF 原件` : "请先保存简历";
    const container = $("ai-candidates");
    container.replaceChildren();
    $("ai-draft-info").textContent = "";
    if (!draft) {
      $("ai-draft-info").textContent = "还没有提取草稿。也可直接手工新建项目。";
      return;
    }
    const usage = draft.input_tokens === null && draft.output_tokens === null ? "" :
      ` · 输入 ${draft.input_tokens ?? "—"} / 输出 ${draft.output_tokens ?? "—"} tokens`;
    $("ai-draft-info").textContent = `AI 候选 · ${labels[draft.status]} · 固定依据简历 v${draft.source_version}。只有逐项核对并保存后才成为正式项目。`;
    const technical = document.createElement("details");
    technical.className = "ai-technical";
    technical.append(text("summary", "提取任务详情"), text("p", `${draft.model}${draft.elapsed_ms === null ? "" : ` · ${(draft.elapsed_ms / 1000).toFixed(1)} 秒`}${usage}`));
    container.append(technical);
    if (draft.status === "running") {
      container.append(text("p", "本次提取仍在处理，可点击「刷新结果」。读取结果不会再次调用模型。", "material-help"));
      return;
    }
    if (draft.status === "failed") {
      container.append(text("p", `${draft.error_message || "本次提取失败"}（${draft.error_code || "unknown"}）。点击「AI 提取项目」会发起新调用。`, "ai-warning"));
      return;
    }
    const result = draft.result;
    for (const warning of result?.needs_confirmation || []) container.append(text("p", warning, "ai-warning"));
    if (!result?.projects.length) container.append(text("p", "没有找到带可匹配证据的项目，请核对简历文字或手工整理。", "material-help"));
    for (const [index, project] of (result?.projects || []).entries()) {
      const saved = draft.confirmed_projects[String(index)];
      const card = text("article", "", "ai-candidate");
      const heading = text("div", "", "project-toolbar");
      const action = text("button", saved ? "已保存为项目" : "载入核对", "ds-button ds-button--small");
      action.type = "button";
      action.disabled = !!saved;
      action.addEventListener("click", () => void run(() => onLoadCandidate(draft, index)));
      heading.append(text("h4", project.name), action);
      card.append(heading, text("p", `${saved ? "用户已确认" : "AI 候选，待人工核对"} · ${project.facts.length} 条事实 · 依据简历 v${draft.source_version}`, "material-help"));
      if (project.summary) card.append(text("p", project.summary));
      for (const warning of project.needs_confirmation) card.append(text("p", warning, "ai-warning"));
      const evidence = document.createElement("details");
      evidence.append(text("summary", "项目原文依据与提取信息"), text("pre", project.evidence_quote), text("p", `模型自评 ${Math.round(project.confidence * 100)}%（不代表真实性）`, "material-help"));
      card.append(evidence);
      for (const [factIndex, fact] of project.facts.entries()) {
        const item = document.createElement("details");
        item.append(text("summary", `候选事实 ${factIndex + 1} · ${fact.fact_type}`), text("p", fact.content),
          text("p", `模型自评 ${Math.round(fact.confidence * 100)}% · 原文引用：`, "material-help"),
          text("pre", fact.evidence_quote));
        for (const warning of fact.needs_confirmation) item.append(text("p", warning, "ai-warning"));
        card.append(item);
      }
      container.append(card);
    }
  }

  async function loadDrafts(resumeId) {
    if (state.resumeId !== resumeId) reset(resumeId);
    try {
      const data = await request(`${path()}?limit=10&offset=${state.offset}`);
      state.items = data.items;
      state.total = data.total;
      if (!state.items.some((item) => item.id === state.selectedId)) state.selectedId = state.items[0]?.id || null;
      const draft = current();
      if (state.pending) {
        const pending = state.items.find((item) => item.id === state.pending.request_id);
        if (pending && pending.status !== "running") state.pending = null;
      } else if (draft?.status === "running") {
        state.pending = { request_id: draft.id, source_version: draft.source_version };
      }
      render();
    } catch {
      $("ai-draft-info").textContent = "提取草稿读取失败，点击「刷新结果」重试；手工项目编辑仍可使用。";
    }
  }

  function reset(resumeId = null) {
    Object.assign(state, { resumeId, items: [], selectedId: null, pending: null, offset: 0, total: 0 });
    render();
  }

  async function generate() {
    const resume = getResume();
    if (!resume) return;
    if (!state.pending && isResumeDirty()) {
      notify("请先保存简历修改，再提取项目；模型只读取已保存的简历版本。", true);
      return;
    }
    if (state.resumeId !== resume.id) reset(resume.id);
    if (!state.pending && current()?.status === "running") {
      state.pending = { request_id: current().id, source_version: current().source_version };
    }
    if (!state.pending) state.pending = { request_id: crypto.randomUUID(), source_version: resume.content_version };
    render();
    notify("正在读取或提取项目。结果会保存为草稿，暂不创建项目；请稍候。");
    const draft = await progress("正在提取 / 读取候选项目，尚未保存正式项目", () => request(path(), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state.pending), timeoutMs: 330000,
    }));
    if (draft.status !== "running") state.pending = null;
    state.selectedId = draft.id;
    state.offset = 0;
    await loadDrafts(resume.id);
    notify(draft.status === "failed" ? draft.error_message : draft.status === "running" ?
      "提取仍在处理中，可刷新结果。" : "提取草稿已保存。逐个载入编辑，核对后再保存为项目。", draft.status === "failed");
  }

  listen("ai-extract-projects", "click", generate);
  listen("ai-refresh-drafts", "click", async () => {
    await modelStatus();
    if (getResume()) await loadDrafts(getResume().id);
  });
  listen("ai-draft-select", "change", () => { state.selectedId = $("ai-draft-select").value; render(); });
  listen("ai-draft-previous", "click", async () => { state.offset = Math.max(0, state.offset - 10); await loadDrafts(state.resumeId); });
  listen("ai-draft-next", "click", async () => { state.offset += 10; await loadDrafts(state.resumeId); });
  void modelStatus();
  reset();
  return { loadDrafts, reset };
}
