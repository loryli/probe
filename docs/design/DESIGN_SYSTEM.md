# 练习场 Design System 1.0

日期：2026-11-10。适用原生 HTML、CSS、JavaScript ES Modules。基于已确认的 [UX Architecture](UX_ARCHITECTURE.md)；基础样式与通用组件先行落地，四个业务页随后逐页接入：题库、简历与项目、面试计划、模拟面试均已改用独立样式链，不再加载旧的 styles/theme/workspace 三件套。

展示入口：`/assets/design-system.html`。展示内容只在内存中交互，不保存题目、简历或场次，不调用业务 API 或外部模型。

## 1. 文件责任与加载顺序

位置：`src/interview_assistant/web/`。

| 文件 | 职责 |
|---|---|
| tokens.css | 颜色、字体、尺寸、间距、圆角、阴影、动效变量；声明级联层顺序 |
| base.css | ds-root 范围的基础排版重置、焦点、hidden、减少动效 |
| typography.css | 字阶、正文、原文、代码阅读 |
| layout.css | 容器、排列工具、阅读/计划/专注宽度；不预设所有页面三栏 |
| components.css | 按钮、输入、浮层、标签、通知及状态组件 |
| components.js | 标签页键盘行为、模态打开与焦点恢复、按钮等待、通知创建 |
| design-system.css | 仅展示页目录、色板和样例布局 |
| design-system.html / .js | 独立演示与事件绑定，不含业务请求 |

CSS 按表中前五项及展示样式顺序加载。层顺序为 `ds.tokens → ds.base → ds.typography → ds.layout → ds.components → ds.showcase`。变量放在 `.ds-root` 上，类采用 `ds-` 前缀。现有 styles/theme/workspace 及业务 CSS 保留原样；新页面不混载它们，以免旧的无层规则优先于新层规则。

保留平铺文件以复用现有 `web/*.css` 和 `web/*.js` 包数据声明；本轮不为目录形式迁移旧资源或新建空 pages/。以后每页实施时再增加有实际内容的页面样式。

## 2. Color Tokens

以下变量均使用 `--ds-` 前缀。

| Token | 值 | 用途 |
|---|---|---|
| bg | #F8F6F2 | 页面底色 |
| surface | #FFFFFF | 阅读与编辑表面 |
| surface-muted | #F1EDE7 | 次要表面、只读输入 |
| text | #292B2E | 主文字 |
| text-secondary | #5F5852 | 辅助正文 |
| text-muted | #756B62 | 注释、占位提示 |
| primary | #C46B4E | 品牌强调，不直接作为所有小字号文字的背景 |
| primary-soft | #FAEEE7 | 轻强调区域 |
| action / hover / active | #9F4E35 / #87412D / #733725 | 操作文字和主按钮 |
| on-action | #FFFFFF | 主按钮文字 |
| border | #E8E2D9 | 内容分割线 |
| border-control | #9C9185 | 需要清晰可识别的控件边框 |
| focus | #87412D | 3px 可见键盘焦点环 |
| success / success-bg | #43573F / #EEF2EB | 成功状态 |
| warning / warning-bg | #765526 / #FBF4E5 | 待核对或提醒 |
| danger / danger-bg | #963E32 / #FBEFEC | 错误及危险操作 |
| info / info-bg | #465A6B / #EDF2F6 | 中性进度提示 |
| overlay | #292B2E66 | 模态遮罩 |

状态始终同时使用文字，不靠颜色认定已保存、已核对或失败。轻边框是内容分隔，不能作为控件唯一识别信号。品牌强调与操作色区分是为了保持视觉方向并改善小字可读性；本轮不宣称完成全量无障碍认证。

## 3. Typography Scale

根字号跟随浏览器设置，默认16px；字号采用rem。无远程字体依赖。

| 角色/类 | 默认尺寸 | 行高 | 字体 |
|---|---|---|---|
| ds-display | 32–48px，自适应 | 1.3 | 本机中文衬线回退，少量使用 |
| ds-title | 32px | 1.4 | 界面无衬线 |
| ds-heading | 24px | 1.5 | 界面无衬线 |
| ds-subheading | 18px | 1.5 | 界面无衬线 |
| ds-body | 16px | 1.85 | 界面无衬线 |
| ds-prose | 16px | 1.95 | 长正文，上限880px |
| ds-caption | 14px | 1.7 | 可读辅助说明 |
| ds-overline | 12px | 继承 | 短标签，不承载关键说明 |
| ds-code | 14px | 1.75 | Consolas / SFMono-Regular / monospace |

界面字体：Segoe UI、PingFang SC、Microsoft YaHei；编辑式字体：Noto Serif CJK SC、Source Han Serif SC、SimSun。字体缺失时按本机回退，不承诺所有设备显示一致。`ds-raw` 保留原文空白和换行，不做Markdown解析。`ds-code` 仅局部横向滚动。数字使用 `ds-numeric` 等宽数字特性。

## 4. Spacing、Radius、Shadows

间距刻度：4/8/12/16/20/24/32/40/48/64px，对应 `space-1/2/3/4/5/6/8/10/12/16`。

- 4–8px：标签与控件内部关系；12–16px：同组控件；24–32px：内容分组；40–64px：主要章节。
- 圆角：`radius-sm=4px`（标签、代码）、`radius-md=6px`（控件、普通表面）、`radius-lg=10px`（弹窗）。仅spinner使用圆形，不堆叠胶囊卡片。
- 普通内容无阴影；浮层/通知用 `shadow-overlay=0 16px 56px #292B2E1F`。边框优先表达层级，阴影只解释浮起关系。
- 动效基础140ms，限背景/边框等反馈；减少动效偏好下停用动画与过渡。等待仍有文字说明。

## 5. Layout Rules

| 项目 | 规范 |
|---|---|
| 页面最大宽度 | 1440px，包括两侧内边距 |
| 左右边距 | ≥1280px：40px；900–1279px：24px；<900px：16px |
| 顶部导航 | 最小64px，内容换行时自然增高，不锁死高度 |
| 分类导航 | 桌面参考180px，可收起 |
| 题目列表 | 桌面300px；中屏建议280px，由页面实现 |
| 资料导航 | 桌面240px |
| 计划主体 | 最大960px |
| 内容最大阅读宽度 | 880px |
| 面试专注主体 | 800px |
| 模态辅助抽屉 | 桌面380px；<600px全宽 |

主体内容自然增长、随文档滚动，不设置统一520px高外框。桌面列表允许独立滚动，代码/PDF/浮层是局部滚动例外。窄屏不硬塞三栏；先收辅助，再切换列表/详情；业务布局实施依据四份 UX 文档。宽屏非模态辅助面板和模态抽屉不能共享焦点锁策略。

## 6. 组件规范

| 组件 | 规范 |
|---|---|
| Button | primary、默认secondary、quiet、danger；默认44px高；small36px仅紧凑桌面场景，触屏主操作用默认尺寸 |
| Input | 可见label；帮助与错误通过aria-describedby关联；单行/select最小44px；textarea最小144px可拉高 |
| Dialog | 原生dialog.showModal；最大520px宽，视口减32px；默认聚焦取消等安全操作；Esc关闭、焦点返回 |
| Drawer | 原生模态dialog，右侧380px、移动全屏；头尾固定、内容独立滚动；非模态并列面板另行实施 |
| Tabs | 横向自动激活；左右方向键、Home/End、roving tabindex；跳过disabled；仅用于不触发付费调用的本地视图 |
| Badge | neutral/success/warning/info/danger；文字状态，非可点击筛选器 |
| Notification | 持续inline notice与非阻断toast分开；toast手动关闭，不自动消失、不抢焦点；关键错误留在任务旁 |
| Empty | 简短标题、具体原因、必要动作；区分从未创建与空筛选，无巨幅插画或固定空高 |
| Loading | spinner + 真实阶段文字；按钮disabled/aria-busy；未知进度不用伪百分比 |
| Error | 字段错误贴近输入；任务错误使用inline notice；保留输入与恢复操作，不由组件发起重试 |

Hover只用于可操作控件；Focus使用统一可见轮廓；Active表达按下或标签选中；Disabled保留可读文字；Loading保留文字且防重复激活。静态徽标、空态、排版等非交互组件不添加虚假的Hover/Active。

## 7. 接入策略与验证边界

四个业务页已各自加载 tokens/base/typography/layout/components + `workspace-ui.css` + 页面样式，页面样式负责布局与状态。后续调整仍需同时迁移 HTML 类、布局责任和状态绑定，避免把新 CSS 叠加到旧 CSS 后继续覆盖。组件不管理 revision、幂等键、数据事务、原文或收费调用。

本轮已用临时SQLite启动项目，并初步打开展示页、获取桌面截图；之后按用户新要求停止进一步检查。完整键盘、弹窗、移动端和无障碍验收未完成，不报告为全部通过。没有调用外部模型或修改个人数据库。
