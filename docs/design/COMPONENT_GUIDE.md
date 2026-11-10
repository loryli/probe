# 通用组件使用指南

位置：`src/interview_assistant/web/`。先阅读 [Design System](DESIGN_SYSTEM.md)。展示地址 `/assets/design-system.html`；示例不连接业务数据。

## 1. 页面接入

使用独立样式链，外层标记 `.ds-root`。加载顺序：tokens.css、base.css、typography.css、layout.css、components.css，最后为该页面的专用样式。不要与现有styles/theme/workspace整套混载；旧业务页面需要单独迁移，不靠添加ds-root一键重设计。

```html
<body class="ds-root">
  <main class="ds-container">
    <section class="ds-reading ds-prose">完整阅读内容</section>
  </main>
</body>
```

布局工具：`ds-container`限制页面；`ds-stack`纵向排列；`ds-cluster`换行操作组；`ds-split`双列，<900px单列；`ds-reading / ds-focus / ds-plan`分别限制阅读、训练、计划的宽度。工具不持有业务对象和固定滚动高度。

## 2. 按钮与等待状态

```html
<button type="button" class="ds-button ds-button--primary">保存修改</button>
<button type="button" class="ds-button">取消</button>
<button type="button" class="ds-button ds-button--quiet">查看来源</button>
<button type="button" class="ds-button ds-button--danger">删除题目</button>
```

```js
import { setButtonLoading } from "./components.js";
setButtonLoading(button, true, "正在保存…");
// 请求、事务和错误恢复属于业务调用方；请求完成后恢复呈现。
setButtonLoading(button, false);
```

函数保存原子节点与disabled状态，等待时设disabled及aria-busy，结束后还原；不自动重试，不代表已保存。业务权限或并发状态变化时，调用方在还原后再次计算按钮可用性。表单提交使用type=submit，其余显式type=button。链接导航使用a，不能用aria-disabled代替阻止导航。

## 3. 输入、选择与校验

```html
<div class="ds-field">
  <label class="ds-label" for="title">题目名称</label>
  <input id="title" class="ds-input" aria-describedby="help error">
  <span id="help" class="ds-caption">说明用途与限制。</span>
  <span id="error" class="ds-field-error" hidden>请填写名称。</span>
</div>
```

错误时设置aria-invalid=true、显示错误并定位字段；重新编辑后清除旧错误，最终校验仍由业务负责。textarea/select使用ds-input。readonly允许聚焦、选择、复制，disabled不可编辑且跳过Tab。checkbox/radio包在ds-choice标签里；同组radio使用同一name与fieldset/legend。加载中的表单应在业务容器设置aria-busy并说明原因，不在本组件层锁住整个应用。

## 4. 弹窗和抽屉

```html
<dialog id="confirm" class="ds-dialog" aria-labelledby="confirm-title">
  <div class="ds-dialog-head"><h2 id="confirm-title" class="ds-heading">确认操作</h2></div>
  <div class="ds-dialog-body">说明影响对象和结果。</div>
  <div class="ds-dialog-foot"><button type="button" class="ds-button" autofocus>取消</button></div>
</dialog>
```

```js
import { openOverlay } from "./components.js";
openOverlay(dialog);
// 由自己的取消/关闭按钮触发：
dialog.close();
```

原生showModal提供模态焦点约束与Esc关闭。openOverlay保存body滚动状态、关闭后恢复触发焦点；传入`{returnFocus: textarea}`可恢复选区。默认不点击遮罩关闭、不支持嵌套模态，不自动保存表单。若请求中需要禁止关闭，业务必须同时处理dialog的cancel事件与自定义关闭按钮，不只禁用一个按钮。

抽屉使用`ds-dialog ds-drawer`，同样分head/body/foot。这是**模态覆盖抽屉**；宽屏边读边答的非模态面板应使用aside和独立交互，不调用openOverlay，也不锁住主区。移动端抽屉全屏，关闭入口常驻。

## 5. 标签页

结构为一个root容器，内部一个role=tablist，若干role=tab按钮和role=tabpanel面板。tab.id与面板aria-labelledby对应，tab.aria-controls指向面板ID；初始一项aria-selected=true，其余tabindex=-1；隐藏面板加hidden。

```js
import { initTabs } from "./components.js";
const disposeTabs = initTabs(root);
root.addEventListener("ds:tabchange", (event) => {
  // event.detail.id 是已选tab的ID。不要因此直接调用收费服务。
});
// 移除视图时解除监听：
disposeTabs();
```

每个root初始化一次；左右键循环可用项，Home/End到首尾，disabled跳过。默认自动激活，只适合可即时展示的本地内容；需要远端加载/草稿丢弃确认的业务不能直接绑定自动切换，应先另行设计受控激活行为。

## 6. 徽标、通知与空态

`ds-badge`及`--success/--warning/--info/--danger`变体只作状态文本，不代表模型判断已证实。

`ds-notice`为inline提示，变体同上。短标题说明事实，正文说明影响和下一步；任务失败应在这里保持可见。动态重大错误可在具体区域使用role=alert，普通进度用role=status；不要对所有静态样例添加live region。

```html
<div id="notifications" class="ds-toast-region"
     aria-live="polite" aria-relevant="additions" aria-label="操作通知"></div>
```

```js
import { notify } from "./components.js";
notify(region, "内容已保存。", { tone: "success", title: "保存成功" });
```

notify只用textContent构造文字，不插入用户HTML；不自动抢焦点、不自动消失。关闭当前有焦点通知时回到触发点。调用方应避免重复发同一通知及无上限堆积；不能把此容器当实时日志，也不在弹窗背后用toast承载关键错误。

`ds-empty`用标题、原因及按需按钮组合；无资料时指向新建/导入，无搜索结果时清除条件，读取失败不伪装空数据。加载使用ds-loading、ds-spinner与具体阶段文字，spinner加aria-hidden=true；动态文字可以放role=status，转圈不是唯一反馈。

## 7. 展示页体验步骤

启动后访问 `/assets/design-system.html`：

1. 查看五个基础色、字体层级、长正文和局部代码滚动。
2. 用鼠标和Tab体验按钮，点击“演示等待状态”观察1.5秒演示及恢复。
3. 空名称点击“检查输入”，观察字段错误、焦点及保留输入；填入名称后再检查。
4. 打开确认弹窗/抽屉，用Tab和Esc体验关闭与焦点返回。
5. 标签页按左右、Home、End，确认跳过禁用项；检查对应面板。
6. 查看通知、空态、加载和失败反馈，切换窄屏体验自然换行与全屏抽屉。

演示计时器只存在展示页，不能移到业务页当作真实请求成功信号。没有执行全量组件验收；按用户最新要求停止后续检查，由用户体验反馈。
