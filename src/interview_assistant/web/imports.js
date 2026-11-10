import { openOverlay } from "./components.js";

// Import UI owns its draft selection; the server owns extracted text and source.
export function setupMarkdownImport({ request, onImported, canOpen = () => true }) {
  const $ = (id) => document.getElementById(id);
  const dialog = $("import-dialog");
  const root = "/question-bank/imports";
  let preview = null;
  let selected = new Set();
  let current = null;
  let busy = false;
  let pending = null;
  const strategies = { heading_3: "按三级标题", heading_2: "按二级标题", qa: "按 Q/A 标记" };

  function element(tag, content, className) {
    const node = document.createElement(tag);
    node.textContent = content;
    if (className) node.className = className;
    return node;
  }

  function message(content, error = false) {
    $("import-message").textContent = content;
    $("import-message").classList.toggle("error", error);
    $("import-message").hidden = !content;
  }

  function updateControls() {
    const confirmed = preview?.status === "confirmed";
    for (const id of ["markdown-file", "import-strategy", "parse-markdown"]) $(id).disabled = busy || !!pending;
    for (const id of ["import-close", "import-cancel"]) $(id).disabled = busy;
    for (const id of ["import-recommended", "import-select-all", "import-select-none"]) $(id).disabled = busy || !!pending || confirmed;
    for (const input of $("import-candidates").querySelectorAll("input")) {
      input.disabled = busy || !!pending || confirmed || input.dataset.empty === "true";
    }
    $("confirm-import").disabled = busy || !preview || confirmed || selected.size === 0;
    $("confirm-import").textContent = busy ? "正在处理…" : confirmed ? "已导入" : pending ? "重试确认" : `确认导入${selected.size ? ` ${selected.size} 道题` : ""}`;
    $("import-cancel").textContent = confirmed ? "返回题库" : "关闭预览";
    $("import-selected-count").textContent = `已选 ${selected.size} / ${preview?.candidates.length || 0} 个候选`;
  }

  function setBusy(value) { busy = value; updateControls(); }

  function setStep(step) {
    for (const name of ["file", "preview", "confirm"]) {
      const node = $("import-step-" + name);
      node.classList.toggle("active", step === name);
      if (step === name) node.setAttribute("aria-current", "step"); else node.removeAttribute("aria-current");
    }
  }

  function invalidate() {
    preview = null;
    selected = new Set();
    current = null;
    dialog.classList.remove("candidate-open");
    $("import-preview").hidden = true;
    message("");
    setStep("file");
    updateControls();
  }

  function showCandidate(candidate, focus = false) {
    current = candidate.index;
    $("candidate-location").textContent = `${candidate.heading_path.join(" / ") || "问答笔记"} · 第 ${candidate.start_line}–${candidate.end_line} 行`;
    $("candidate-question").textContent = candidate.question_text || "（空题干，不能导入）";
    $("candidate-answer").textContent = candidate.standard_answer_text || "（未填写，不会自动补写）";
    $("candidate-raw").textContent = candidate.raw_text;
    $("candidate-warnings").replaceChildren(...candidate.warnings.map((warning) => element("li", warning)));
    for (const node of $("import-candidates").children) {
      const active = Number(node.dataset.index) === current;
      node.classList.toggle("active", active);
      node.querySelector("button")?.setAttribute("aria-pressed", String(active));
    }
    if (focus && window.matchMedia("(max-width: 899px)").matches) {
      dialog.classList.add("candidate-open");
      document.querySelector(".import-candidate-detail").focus();
      document.querySelector(".import-candidate-detail").scrollIntoView({ block: "start" });
    }
  }

  function renderCandidates() {
    $("import-candidates").replaceChildren();
    for (const candidate of preview.candidates) {
      const row = element("div", "", "import-candidate-row");
      row.dataset.index = candidate.index;
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = selected.has(candidate.index);
      input.dataset.empty = String(!candidate.question_text.trim());
      input.setAttribute("aria-label", `选择候选 ${candidate.index + 1}：${candidate.question_text}`);
      input.addEventListener("change", () => {
        if (input.checked) selected.add(candidate.index); else selected.delete(candidate.index);
        updateControls();
      });
      const button = element("button", "", "import-candidate-button");
      button.type = "button";
      button.append(element("small", `${candidate.index + 1} · 第 ${candidate.start_line}–${candidate.end_line} 行`));
      button.append(element("span", candidate.question_text || "空题干"));
      if (candidate.warnings.length) button.append(element("small", `${candidate.warnings.length} 条提示`, "candidate-warning-count"));
      button.addEventListener("click", () => showCandidate(candidate, true));
      row.append(input, button);
      $("import-candidates").append(row);
    }
    if (preview.candidates.length) showCandidate(preview.candidates.find((item) => item.index === current) || preview.candidates[0]);
    else {
      $("import-candidates").append(element("p", "没有可预览的候选题。", "panel-description"));
      for (const id of ["candidate-location", "candidate-question", "candidate-answer", "candidate-raw"]) $(id).textContent = "";
      $("candidate-warnings").replaceChildren();
    }
    updateControls();
  }

  function showPreview(data) {
    preview = data;
    pending = null;
    dialog.classList.remove("candidate-open");
    selected = new Set(data.status === "confirmed" ? data.selected_indices : data.candidates.filter((item) => item.recommended).map((item) => item.index));
    $("import-preview").hidden = false;
    $("import-file-name").textContent = data.file_name;
    $("import-summary-text").textContent = `${strategies[data.resolved_strategy]} · 识别到 ${data.candidates.length} 个候选段落`;
    $("import-download").href = `${root}/${data.id}/document`;
    $("import-warnings").replaceChildren(...data.warnings.map((warning) => element("li", warning)));
    setStep(data.status === "confirmed" ? "confirm" : "preview");
    if (data.status === "confirmed") message(`此文件在当前识别方式下已导入 ${data.selected_indices.length} 道题，不会再次创建。修改文件或识别方式会形成另一批次，请避免重复导入。`);
    renderCandidates();
  }

  async function parseFile() {
    if (busy || pending) return;
    const file = $("markdown-file").files[0];
    if (!file) { message("请先选择 Markdown 文件。", true); return; }
    if (!/\.(md|markdown)$/i.test(file.name) || file.size > 1024 * 1024) {
      message("请选择不超过 1 MiB 的 .md 或 .markdown 文件。", true); return;
    }
    setBusy(true);
    message("正在读取文件并生成预览…");
    try {
      let content;
      try {
        content = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(await file.arrayBuffer());
      } catch { throw new Error("文件不是有效 UTF-8 编码，请先在编辑器中另存为 UTF-8 后重试。"); }
      if (!content.length || content.includes("\0")) throw new Error("文件为空或含空字符，请检查文件内容。");
      const data = await request(root, { method: "POST", body: JSON.stringify({ file_name: file.name, content, strategy: $("import-strategy").value }) });
      message("");
      showPreview(data);
    } catch (error) { message(error.message || "预览生成失败，请重试。", true); }
    finally { setBusy(false); }
  }

  async function confirmImport() {
    if (busy || !preview || preview.status === "confirmed" || !selected.size) return;
    pending ||= { selected_indices: [...selected].sort((a, b) => a - b) };
    setBusy(true);
    message("正在保存选中的题目…");
    try {
      const result = await request(`${root}/${preview.id}/confirm`, { method: "POST", body: JSON.stringify(pending) });
      preview.status = "confirmed";
      preview.selected_indices = pending.selected_indices;
      pending = null;
      setStep("confirm");
      message(`导入完成：${result.imported_count} 道题目已保存，可关闭预览回到题库。${result.replayed ? "本次为安全重试，没有重复创建。" : ""}`);
      await onImported(result);
    } catch (error) {
      if (error.status === 409) {
        try { showPreview(await request(`${root}/${preview.id}`)); } catch { /* Keep the frozen selection for retry. */ }
      } else if (error.status && error.status < 500) pending = null;
      message(`${error.message || "确认未完成。"}${pending ? " 本次选择已锁定，点击「重试确认」可安全重试。" : ""}`, true);
    } finally { setBusy(false); }
  }

  function choose(mode) {
    if (busy || pending || !preview || preview.status === "confirmed") return;
    selected = new Set(preview.candidates.filter((item) => item.question_text.trim() && (mode === "all" || mode === "recommended" && item.recommended)).map((item) => item.index));
    renderCandidates();
  }

  $("import-markdown").addEventListener("click", () => { if (canOpen()) { openOverlay(dialog); updateControls(); } });
  $("back-to-candidates").addEventListener("click", () => {
    dialog.classList.remove("candidate-open");
    const row = [...$("import-candidates").children].find((node) => Number(node.dataset.index) === current);
    row?.querySelector("button")?.focus();
  });
  for (const id of ["import-close", "import-cancel"]) $(id).addEventListener("click", () => { if (!busy) dialog.close(); });
  dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
  $("markdown-file").addEventListener("change", invalidate);
  $("import-strategy").addEventListener("change", invalidate);
  $("parse-markdown").addEventListener("click", parseFile);
  $("confirm-import").addEventListener("click", confirmImport);
  $("import-recommended").addEventListener("click", () => choose("recommended"));
  $("import-select-all").addEventListener("click", () => choose("all"));
  $("import-select-none").addEventListener("click", () => choose("none"));
  window.addEventListener("beforeunload", (event) => {
    if (busy || pending) { event.preventDefault(); event.returnValue = ""; }
  });
}
