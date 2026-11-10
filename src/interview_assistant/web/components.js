// Presentation only. No requests, persistence, business state or automatic retries.
const loading = new WeakMap();

export function setButtonLoading(button, busy, label = "正在处理…") {
  if (busy) {
    if (loading.has(button)) return;
    loading.set(button, { nodes: [...button.childNodes], disabled: button.disabled });
    button.textContent = label;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  } else {
    const saved = loading.get(button);
    if (!saved) return;
    button.replaceChildren(...saved.nodes);
    button.disabled = saved.disabled;
    button.removeAttribute("aria-busy");
    loading.delete(button);
  }
}

export function initTabs(root) {
  const list = root.querySelector('[role="tablist"]');
  const tabs = [...list.querySelectorAll('[role="tab"]')];
  const select = (selected, focus = false) => {
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      root.querySelector(`#${CSS.escape(tab.getAttribute("aria-controls"))}`).hidden = !active;
    }
    if (focus) selected.focus();
    root.dispatchEvent(new CustomEvent("ds:tabchange", { detail: { id: selected.id } }));
  };
  const controller = new AbortController();
  const options = { signal: controller.signal };
  list.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tabs.includes(tab) && !tab.disabled) select(tab);
  }, options);
  list.addEventListener("keydown", (event) => {
    const enabled = tabs.filter((tab) => !tab.disabled);
    const index = enabled.indexOf(document.activeElement);
    if (index < 0 || !enabled.length) return;
    let next;
    if (event.key === "ArrowRight") next = enabled[(index + 1) % enabled.length];
    if (event.key === "ArrowLeft") next = enabled[(index - 1 + enabled.length) % enabled.length];
    if (event.key === "Home") next = enabled[0];
    if (event.key === "End") next = enabled.at(-1);
    if (next) { event.preventDefault(); select(next, true); }
  }, options);
  const initial = tabs.find((tab) => !tab.disabled && tab.getAttribute("aria-selected") === "true")
    || tabs.find((tab) => !tab.disabled);
  if (initial) select(initial);
  return () => controller.abort();
}

export function openOverlay(dialog, { returnFocus = document.activeElement } = {}) {
  if (dialog.open || document.querySelector("dialog:modal")) return;
  const selection = returnFocus instanceof HTMLTextAreaElement
    ? [returnFocus.selectionStart, returnFocus.selectionEnd] : null;
  const previousOverflow = document.body.style.overflow;
  dialog.showModal();
  document.body.style.overflow = "hidden";
  dialog.addEventListener("close", () => {
    document.body.style.overflow = previousOverflow;
    if (returnFocus?.isConnected) {
      returnFocus.focus({ preventScroll: true });
      if (selection) returnFocus.setSelectionRange(...selection);
    }
  }, { once: true });
}

export function notify(region, message, { tone = "info", title = "提示", returnFocus = document.activeElement } = {}) {
  const allowed = new Set(["info", "success", "warning", "danger"]);
  const item = document.createElement("div");
  item.className = `ds-notice ds-notice--${allowed.has(tone) ? tone : "info"} ds-toast`;
  const content = document.createElement("div");
  content.className = "ds-stack";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const text = document.createElement("p");
  text.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "ds-button ds-button--quiet";
  close.textContent = "关闭";
  close.setAttribute("aria-label", `关闭通知：${title}`);
  close.addEventListener("click", () => {
    const hadFocus = item.contains(document.activeElement);
    item.remove();
    if (hadFocus && returnFocus?.isConnected) returnFocus.focus();
  });
  content.append(heading, text);
  item.append(content, close);
  region.append(item);
  return item;
}
