# 练习场：现有前端与业务审计

日期：2026-11-10。方法：静态阅读 HTML、CSS、ES Modules、相关路由与业务契约，对照 README 和 AGENTS.md。没有启动服务、调用接口或模型，也没有运行测试、静态检查或浏览器验证。下文的实现事实不等于运行验收结论。

本轮开始时 `git status --short` 无输出。审计以当前工作区为准，不沿用早期“文字面试尚未实现”的结论。不读取个人数据库、上传材料或密钥。

## 1. 架构和入口

FastAPI 在 [main.py](../../src/interview_assistant/main.py) 提供四个 HTML 页面及 `/assets` 静态目录。每页独立加载模块，无客户端路由、前端构建器或框架状态库。Fetch 同源访问数据库持久化接口。HTML 响应有 `Cache-Control: no-cache`，主要 CSS/入口模块使用手工查询版本 `v=20261110-4`。

| 页面 | 入口与脚本 | 当前结构与业务 |
|---|---|---|
| 个人题库 | `/`；`index.html` → `app.js` | 分类、题目、内容三栏；分类创建/改名、本页勾选移动；搜索、启停筛选、分页；只读/编辑切换；内容历史与来源；Markdown 导入对话框 |
| 简历与项目 | `/materials`；`materials.html` → `materials.js` | 简历列表 + 工作区；简历文字/项目与事实/历史版本三个标签；PDF/OCR、文字整理与撤销；只读/编辑；AI 提取草稿及逐候选确认 |
| 面试计划 | `/planner`；`planner.html` → `planner.js` | 历史列表 + 配置/阅读/结果；选择版本、项目与题库，模型/规则模式；生成、读取、确认、删除；覆盖率、预算和引用详情 |
| 模拟面试 | `/interview`；`interview.html` → `interview.js` | 欢迎/开始/面试房间；当前问题与回答；暂停、继续、跳过目标、结束、重试；抽屉承载记录、目标、参考答案、历史及详情；结束后复盘 |

已有顶栏导航、主内容跳转链接、原生 label/form、部分 tab/tabpanel、dialog、details、状态提示和 `hidden` 分区。四页导航结构仍分别写在 HTML 中，尚无统一壳模块。CSS 中仍有旧 sidebar 规则，不据此认定可以直接删除。

## 2. 模块与状态所有权

| 模块 | 状态/行为 | 重设计时的依赖 |
|---|---|---|
| `app.js` | `selected/isNew/editing/busy`、筛选与分页、分类、批量选择、历史；请求序号用于部分旧响应隔离 | 固定 DOM ID；文本域基线比较；实际变化字段 PATCH；创建幂等键；取消草稿保护 |
| `imports.js` | `setupMarkdownImport({request,onImported,canOpen})`；闭包保存预览、选择、忙碌与 pending | 对话框、候选原文、选择锁定；父页面刷新回调；不自建另一套题库数据 |
| `materials.js` | 简历、项目分别保存编辑状态；来源、原件 ID、格式撤销、AI 候选、分页 | `operate()` 设置 main.inert/aria-busy；跨标签草稿；简历保存后保留项目草稿；全文 PUT 的原文恢复 |
| `ai_projects.js` | `setupAiProjects(...)` 注入请求、执行器和候选载入回调；内部提取任务列表与 pending | 读取任务与新调用分开；候选载入人工编辑；模型置信度标为未经核实 |
| `planner.js` | 资料选项、选择集合、历史计划、editing/dirty/busy、pendingId、等待计时 | `configDrift()` 比较配置；`planner.v2` 可用性；确认与重新生成分离；候选池选择固化 |
| `interview.js` | 当前场次、stream/live、报告、计时器、抽屉、引用资料、context、回答选区 | DOM、异步副作用与渲染集中；context 隔离部分迟到响应；保存 ack、流式增量、最终 state 有不同含义 |
| `record_view.js` | `renderRecord(node,{title,meta,fields})` | 三个资料页面复用的只读视图；输入为已保存记录，通过 textContent 展示，不替代编辑草稿 |

这是“页面状态对象 + 函数渲染 + 事件绑定”的模式，不是缺少状态管理。当前没有通过 localStorage/sessionStorage 替代业务数据库；草稿主要在页面内存中，不保证刷新后恢复。

请求封装在四个入口各有一份 `request/ApiError`，通知、日期格式、安全建 DOM 和忙碌状态也有重复。题库默认请求限时 15 秒，其他页面一般 60 秒，PDF/OCR 和模型任务显式使用更长等待。不能抽成一个统一短超时，也不能统一设置 JSON 请求体而破坏 binary PDF 或流请求。

## 3. CSS 加载与覆盖

以下顺序为 HTML 的真实加载顺序；CSS 仍按重要性、特异性、声明顺序共同决定结果。

| 页面 | 加载链 |
|---|---|
| 题库 | `styles.css → theme.css → questions.css → workspace.css` |
| 简历 | `styles.css → materials.css → theme.css → workspace.css` |
| 计划 | `styles.css → planner.css → theme.css → workspace.css` |
| 面试 | `styles.css → interview.css → theme.css → workspace.css` |

- `styles.css`：基础变量、旧导航/布局、控件、编辑区、导入对话框及响应式，已有大量业务选择器，并非纯 reset。
- `theme.css`：已采用本次五个基础色，包含 token、字体回退、状态色、按钮、顶栏；同时覆盖 `.bank`、`.page`、表单、抽屉等布局，职责超出纯主题。
- `questions.css`：题库三栏、分类、批量操作和两个分类相关 dialog；其页面布局加载在主题后。
- `materials.css / planner.css`：各页面表单、事实、目标、候选和响应式，部分规则受后加载的 theme 覆盖。
- `interview.css`：既有早期列表/房间样式，也有后追加的专注界面规则；同文件多次定义 `.interview-page .bank` 等。
- `workspace.css`：最后加载，统一 `--workspace-width:1440px` 与 `--workspace-height:max(520px,calc(100dvh - 300px))`，重新设置三栏/两栏和内部滚动；面试重新套白色边框容器；1150/760px 断点再覆盖。

**具体覆盖实例**：`questions.css` 为页面设置 1600px 最大宽度，`workspace.css` 后用同等特异性的页面选择器改为 1440px；`theme.css` 为面试取消 `.bank` 边框和背景，`workspace.css` 又设置白色表面和边框。这是当前级联事实，不表示某个尺寸已出现可见缺陷。

断点并存 1150/1100/1000/800/760/700/600/360px。固定最小高度、顶栏、内部滚动、sticky 操作栏与抽屉组合，需要后续在小屏、低高度、缩放和软键盘场景人工体验，当前未验证。`[hidden]` 的强制隐藏与 reduced-motion 的 `!important` 是行为支持，不应统一删去。

## 4. 前后端调用关系

下表只用于审计定位，不另立接口规范；最终结构仍由代码生成 `/openapi.json`，在 Apifox 导入。

| 前端行为 | 现有接口族 | 关键约束 |
|---|---|---|
| 题目阅读/编辑、历史 | `/question-bank/questions` 及 `/{id}/versions`、`/{id}/source` | PATCH + expected_revision；创建 Idempotency-Key；删除是逻辑删除 |
| 分类与移动 | `/question-bank/categories`；`POST /question-bank/questions/classify` | 分类创建 request_id；改名 revision；批量移动提交每题 revision，不修改原文内容版本 |
| Markdown 预览/确认 | `/question-bank/imports`、`/{id}/confirm`、`/{id}/document` | 先预览；确认只交候选序号；结果未知时锁定本次选择 |
| PDF 与文字 | `/materials/documents/pdf`、`/{id}/extract`、`/{id}/file`；`/materials/text/format` | PDF binary；auto/force/off；提取和整理返回预览，不等于保存简历 |
| 简历/项目 | `/materials/resumes`、版本、嵌套 projects | 完整内容 PUT；固定来源版本；事实引用验证；保存与核对有独立语义 |
| AI 项目草稿 | `/materials/resumes/{id}/parse-drafts` 及读取、confirm | 显式调用；持久化草稿；人工编辑核对后逐候选保存 |
| 计划 | `/interview-plans/options`、集合、`/{id}`、confirm、runtime | 配置与结果匹配；旧计划保留查看；runtime 提供冻结资料及版本 |
| 面试 | `/interviews/options`、集合、`/{id}`、answers/stream、events、retry、pause/resume/skip/end | 服务端状态为准；保存回答与下一问生成分开；控制动作使用 revision |
| 整场复盘 | `GET/POST /interviews/{id}/report` | 已结束场次；读取与新生成不同；报告失败不丢问答 |
| 本地状态 | `/health/ready`；`/ai/status` | 数据库就绪与模型配置是不同状态；配置检查不调用模型 |

面试提交通过 Fetch 读取 POST SSE：`ack` 表示已落库，`delta` 为显示增量，`state` 为最终快照，`error` 为流内错误；恢复等待还使用 EventSource GET events，失败后轮询 GET 场次。服务端流不是持久化事件日志，不支持逐字事件重放。刷新恢复依据数据库场次快照；不能把临时气泡当最终已保存问题。

## 5. 已有可复用能力与建议边界

**已经存在**：`renderRecord`；Markdown 导入控制器；AI 项目草稿控制器；theme token；按钮/表单/通知/状态徽标/分页/只读块/冲突区的样式惯例；原生 dialog/details。它们有复用价值，但仍依赖固定 DOM ID，不是独立组件库。

**建议逐步提取**：兼容 JSON/binary 的普通请求工具、安全文本 DOM、日期/时长、状态提示、通用分页与编辑模式的呈现。流解析器维持独立；资料来源与面试状态机继续由各领域负责。只在两个真实调用方能够保持行为一致时提取，不用统一大 state 或万能表单替代页面逻辑。

## 6. 风险与不确定事项

| 编号 | 证据/现状 | 影响与处理 |
|---|---|---|
| A01 | theme、页面、workspace 重复定义壳和尺寸 | 后续按选择器迁移，勿新增第五层全局补丁；勿直接删除旧文件 |
| A02 | 四页固定 DOM ID、动态建 DOM、模块初始化绑定 | 改 HTML 可能让事件失效；每页先建立 ID/事件/状态清单 |
| A03 | 多份请求封装，资料页 main.inert，面试 busy/live/reportBusy 分离 | 统一控件外观不能合并不同业务时限、可操作状态 |
| A04 | SSE 增量与最终持久化状态分离，开始/回答使用请求 ID | 断线恢复和结果未知的文案必须保留；前端重试有无复用 ID需后续专项评估，UI 阶段不改变协议 |
| A05 | app/imports 与 materials/ai_projects 子模块 import 没有入口同款版本参数 | 缓存策略不一致的维护风险；尚未观察到浏览器缓存故障 |
| A06 | 参考资料通过 plan runtime 读取，并检查 plan_revision | 计划删除/不可读时参考面板可能失败，场次仍能继续；不能展示当前最新版冒充冻结资料 |
| A07 | `interview.html` 开场说明及 `service.py:BUDGET_GRACE_SEC=300`、决策收尾分支；AGENTS 约定不强制到时结束 | **业务冲突待确认**：五分钟宽限是否后续获用户授权，本轮未发现可证明的决策记录；不自行删改或把它确立成新规则 |
| A08 | 参考答案允许训练中主动展开；无查看答案影响评估的完整规则 | 保持现状，是否区分练习/考试模式待确认；不新增模式或扣分 |
| A09 | 复盘有 solid/mixed/weak 整体标签及逐目标观察；无数值总分 | 不能把标签改成百分制；文字标签的表达后续讨论，不认定为确定能力画像 |
| A10 | 已有焦点样式、标签语义、Esc 抽屉关闭/选区恢复、reduced-motion | 未验证完整 tab 键盘规范、抽屉模态策略、读屏流式播报负担及各状态对比度；不声称符合完整无障碍标准 |
| A11 | 中文字体使用系统回退，长原文以 pre/textarea 呈现 | 字体实际外观、长代码横向阅读、低屏高布局与手机键盘均需人工体验，不能只凭源码定论 |

## 7. 当前结论

已有色彩系统、顶栏、阅读/编辑分离和面试专注视图可以保留演进。下一阶段的主要工作是明确各页面的信息优先级与布局责任，随后收敛级联和公共呈现；不是重复建设已有功能或重写后端。布局方案、组件提取与全部运行体验均尚未验收。
