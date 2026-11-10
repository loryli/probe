import { initTabs, openOverlay, setButtonLoading, notify } from "./components.js";

const $ = (id) => document.getElementById(id);
const toast = (message, tone = "info", title = "组件演示") => notify($("toast-region"), message, { tone, title });
initTabs($("demo-tabs"));
$("open-dialog").addEventListener("click", () => openOverlay($("demo-dialog")));
$("open-drawer").addEventListener("click", () => openOverlay($("demo-drawer")));
document.querySelectorAll("[data-close]").forEach((button) => {
  button.addEventListener("click", () => $(button.dataset.close).close());
});
$("confirm-demo").addEventListener("click", () => {
  $("demo-dialog").close();
  toast("已完成演示，没有修改任何资料。", "success");
});
$("demo-loading").addEventListener("click", (event) => {
  const button = event.currentTarget;
  setButtonLoading(button, true, "正在演示等待…");
  setTimeout(() => {
    setButtonLoading(button, false);
    toast("等待状态已结束。这只是本页的计时演示。", "success");
  }, 1500);
});
$("demo-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("demo-title");
  const invalid = !input.value.trim();
  $("title-error").hidden = !invalid;
  input.setAttribute("aria-invalid", String(invalid));
  $("form-result").textContent = invalid ? "请检查练习名称，输入内容已保留。" : "输入检查完成；内容没有保存或发送。";
  if (invalid) input.focus();
});
$("demo-title").addEventListener("input", () => {
  $("title-error").hidden = true;
  $("demo-title").removeAttribute("aria-invalid");
  $("form-result").textContent = "";
});
$("toast-success").addEventListener("click", () => toast("成功通知样式。点击关闭可收起，不会自动消失。", "success", "操作完成（示例）"));
$("toast-error").addEventListener("click", () => toast("错误通知样式。真实业务还应在任务旁保留原因和恢复入口。", "danger", "操作未完成（示例）"));
for (const id of ["demo-primary", "demo-secondary", "demo-quiet", "demo-danger", "demo-small", "demo-empty", "demo-clear", "demo-retry"]) {
  $(id).addEventListener("click", () => toast("这是组件交互演示，不会保存、删除或调用服务。"));
}
