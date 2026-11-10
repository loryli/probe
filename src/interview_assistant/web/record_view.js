// Read mode uses saved records, never the mutable editor draft.
export function renderRecord(node, { title, meta = "", fields = [] }) {
  const text = (tag, value, className) => {
    const element = document.createElement(tag);
    element.textContent = value;
    if (className) element.className = className;
    return element;
  };
  node.replaceChildren();
  if (title) node.append(text("h3", title, "record-title"));
  if (meta) node.append(text("p", meta, "record-meta"));
  for (const [label, value, options = {}] of fields) {
    const section = document.createElement("section");
    const content = text("pre", value || options.emptyText || "未填写", "record-text");
    if (!value) content.classList.add("record-empty");
    section.append(text("h4", label), content);
    node.append(section);
  }
}
