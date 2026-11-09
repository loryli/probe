# 个人 AI 面试助手

个人自用的 AI 面试练习工具，首发面向大模型应用开发 / AI 全栈。根据简历项目与个人题库动态提问，用带来源证据的记录复盘。

目前已实现 **后端基础骨架、个人题库、简历与项目、面试计划（Planner）与文字模拟面试的页面及 API**：题库 CRUD、内容历史、Markdown 预览导入，PDF 原件保存、文字提取与本地 OCR、简历版本、项目事实维护、Qwen 项目提取草稿与人工确认保存，固定资料版本的考察目标与预算规划，以及按已确认计划逐轮提问、保存回答的文字面试最小闭环。页面直接操作后端保存的数据。实时事件、逐轮分析与复盘、记忆尚未实现。项目设计见 [设计文档入口](docs/README.md)。

## 当前基础架构

- 一个 FastAPI 应用，使用应用工厂和 lifespan 管理资源。
- 原生 HTML/CSS/JavaScript 题库页面，由 FastAPI 同源提供，无需安装 Node.js 或单独启动前端服务。
- Pydantic Settings 读取环境变量和 `.env`，敏感配置使用 SecretStr。
- SQLAlchemy 2.0 异步数据库会话；本地默认 SQLite，支持显式选择 MySQL，事务由业务服务管理。
- SSE 事件编码与流响应工具；尚未提供业务事件订阅、持久化或断线重放。
- 供应商无关的结构化输出协议，以及 Qwen Chat Completions 适配器；请求限时、JSON 校验、调用用量与脱敏错误记录。
- UTC 应用日志、健康检查、离线测试、格式/静态检查和 GitHub Actions 配置。

## 本地启动

需要 Python 3.11 或更新版本。以下命令适用于 PowerShell，从仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c constraints.txt -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn interview_assistant.main:create_app --factory --reload
```

macOS/Linux 使用 `.venv/bin/python` 替代 `.venv\Scripts\python.exe`。仅启动服务可安装 `-c constraints.txt -e .`，无需开发依赖。`pyproject.toml` 声明依赖范围，`constraints.txt` 记录当前验证过的版本；变更依赖时同步更新并重新验证。

默认本地模式无需填写数据库账号或创建数据库。首次启动会自动在 `data/interview.sqlite3` 创建独立的 SQLite 开发库，并通过 Alembic 准备表结构；题目和历史版本在重启后保留。`.env` 是可选配置，现有文件中的 `INTERVIEW_DATABASE_URL` 留空即可使用此模式。

默认监听本机 `127.0.0.1:8000`，访问：

- 题库页面：<http://127.0.0.1:8000/>，在浏览器中直接管理题目。
- 简历与项目：<http://127.0.0.1:8000/materials>，也可从页面导航进入。
- 面试计划：<http://127.0.0.1:8000/planner>，配置资料版本并生成、确认计划。
- 模拟面试：<http://127.0.0.1:8000/interview>，按已确认计划开始文字面试并回看记录。

四个页面的新建操作统一由页面右上角主按钮触发；空状态区域只保留引导文案，不重复放置按钮。
- Apifox 导入地址：<http://127.0.0.1:8000/openapi.json>，操作见下方「使用 Apifox 调试」。
- 进程存活：`GET /health/live`，成功返回 HTTP 200。
- 数据库就绪：`GET /health/ready`，本地数据库准备好后返回 HTTP 200；未配置或无法连接所选数据库时返回 HTTP 503。

不配置模型密钥也可使用题库。数据库就绪检查不代表 Qwen 已接入。

## 使用题库页面

- 点击「新建题目」，填写题干、自己的标准答案和复习笔记后保存；答案与笔记可留空。
- 左侧按题干搜索、按启用状态筛选并分页；选中一道题即可编辑。支持 `Ctrl+Enter` / `⌘+Enter` 保存。
- 「用于面试抽题」开关控制启用状态，点击保存后生效；面试计划会把启用题目作为候选池。
- 「历史版本」查看历次内容原文，切换版本不会覆盖当前编辑内容。删除题目需确认，后端保留历史记录。
- 未保存的内容在切换题目、新建或离开页面时提醒确认；请求失败时保留输入，创建请求重试使用幂等标识。
- 若 Apifox 或其他页面修改了同一道题，保存时会提示版本冲突并保留草稿，可先复制输入再「读取最新内容」。

文本框显示和编辑 Markdown 原文，不渲染其中的 HTML 或图片；仅发送实际改动的字段，避免编辑笔记时改写未触碰的答案换行。页面不内置演示题目，不使用浏览器本地存储代替数据库。

后续按功能同时推进页面和接口：先完成可在页面验证的一条流程，再添加下一项能力。

## 导入 Markdown 笔记

1. 在首页点击「导入 Markdown」，选择 `.md` / `.markdown` 文件。首版支持 UTF-8（含 BOM），单个文件最多 1 MiB、500 个候选段落；其他编码先在编辑器中另存为 UTF-8。
2. 点击「生成预览」。自动识别优先使用三级标题；没有三级标题时，优先识别 Q/A 标记，否则使用二级标题。也可手动选择三级标题、二级标题或 Q/A 模式。
3. 核对候选题干、答案、来源行号与提示；可按建议勾选、全选、全不选或逐题选择。题干为空不能导入；缺少答案不会自动补写。说明性段落、空答案和文件内完全重复的条目默认不选中，可人工核对后选择。
4. 点击「确认导入」，选中题目、第一份内容版本和来源引用在一个事务中保存；任一写入失败均回滚。预览阶段只保存导入草稿，不创建题目，关闭预览也不会确认入库。
5. 在题库选中导入题目，通过「来源原文」查看对应段落或下载完整来源文件。之后编辑题目不改变导入时的来源记录。

识别规则：标题作为题干，保留编号与 Markdown 转义；标题下存在「面试题：」等明确标记时提取该题干。答案保留标准回答标记、代码块、补充说明、Markdown 图片标记及原有换行。代码围栏和缩进代码中的标题、Q/A 标记不作为拆分点；同级或上级标题结束当前段落。前言、分类标题等未入题库的内容仍在完整来源文件中。图片只保留标记，不读取图片、执行 HTML 或调用模型。规则是确定性的文本提取，不是通用 Markdown 语义解析；自动结果不合适时先切换识别方式核对。

导入草稿、源文件全文、SHA-256、解析器版本与来源行号存入所选数据库，不在源码目录另存上传文件。同一内容、同一识别方式与同一解析器版本复用同一批次，文件改名仍沿用首次记录的文件名。每个批次只确认一次，请确认前核对选择；同样选择可安全重试，改选后重复确认返回 HTTP 409。已删除的导入题目不会因重试而恢复。文件或识别方式变化形成新批次，不做跨批次或语义去重。

网络异常导致确认结果未知时，页面保留并锁定本次选择，可点击「重试确认」；重新打开页面后也可选择原文件、使用相同识别方式重新预览，以读取批次状态。

## 简历与项目管理

已有环境需先安装新增的 PDF 依赖，然后按原方式启动或重启服务：

```powershell
.\.venv\Scripts\python.exe -m pip install -c constraints.txt -e .
.\.venv\Scripts\python.exe -m uvicorn interview_assistant.main:create_app --factory --reload
```

本轮未执行依赖安装、数据库迁移、模型下载或任何测试/浏览器验证。PDF/OCR 依赖已记录在依赖文件，但未在本地验证。local SQLite 会在启动服务时自动升级到当前迁移头（见「配置与数据库」）；显式 MySQL 仍需手工运行 Alembic 迁移。

1. 打开 <http://127.0.0.1:8000/materials> 或点击「简历与项目」，选择「新建简历」。
2. 上传 PDF，核对提取文字，填写名称后保存。PDF 最多 10 MiB、50 页；提取文字最多 1 MiB。也可载入 UTF-8 Markdown/TXT，或直接粘贴文字。
3. 默认「自动」模式先提取文字，对有效文字不足 20 个字母/数字/汉字的页面进行本地 OCR；混合 PDF 按页处理，不重复拼接原有页眉。可选「整份 PDF 使用 OCR」处理提取错乱或文字较多但仍有扫描内容的文件，或选「仅提取已有文字」关闭 OCR。加密 PDF 仍需先导出未加密副本。OCR 可能出现错字、数字或顺序错误，请人工核对。[文字提取限制见 pypdf 官方说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)。
4. 上传 PDF 时先保存原件和提取结果，点击「保存简历」才创建简历或新版本。相同 PDF 字节复用同一份原件，并沿用首次文件名；关闭页面不删除上传原件。原始字节不被 OCR 或人工修正改写。已有 PDF 可更改识别方式后点击「重新识别当前 PDF」，核对结果后再保存；未保存前不会更新简历，也不改变已有项目依据。
5. 保存简历后，在「项目与事实」新建项目，维护名称、职责、时间、技术栈、说明和事实。每条事实可记录类型、内容与可选原文引用；引用必须匹配该项目所绑定简历版本中的连续文字。复制导致的换行差异可匹配，入库仍保留来源中的原始换行。无引用的条目明确标为「手工补充」。
6. 可勾选「我已核对」；继续编辑项目会取消该勾选，保存前可重新确认。现在可使用下方 Qwen 提取草稿流程辅助整理，仍需人工核对。OCR 只用于 PDF 文字识别，Markdown 题库中的图片仍不识别。
7. 简历名称、文字或来源变化时原子保存新版本；启停只更新修改版本。历史页可读取旧文字及对应 PDF。项目固定绑定创建时的简历版本，更新简历不会悄悄重绑来源；如需基于新版重新整理，可创建新项目。

OCR 输出根据文本框坐标合并同一视觉行，按标题、条目和长句整理分段，不再把每个文本框直接作为独立段落。对「姓／名：」等有冒号佐证的已知字段修复断行，不修改词句、数字或推测缺失内容。已载入或粘贴的文字也可点击「整理换行」，结果只进入当前草稿，可立即「撤销整理」；手工继续编辑或保存后清除该次撤销入口。复杂多栏、旋转文字和识别杂字符仍需人工核对。

本次升级了提取结果缓存版本，旧 PDF 点击「重新识别当前 PDF」会使用新的排版规则，不复用旧格式结果；已保存的简历与项目依据不会自动修改。未运行识别或页面验证。

简历、项目支持逻辑删除和并发修改保护。删除简历后关联项目从普通接口与页面隐藏，版本、PDF 和项目事实保留；编辑事实时旧事实记录保留，不覆盖来源文字。本阶段提供简历历史页面，不提供项目编辑历史页面。未保存输入在切换资料或离开页面时提醒，失败时保留输入。创建重试沿用同一个 UUID `Idempotency-Key`。

OCR 使用 [RapidOCR 与 ONNX Runtime](https://rapidai.github.io/RapidOCRDocs/main/quickstart/) 在本机执行，由 [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/python_api.html) 将 PDF 页面渲染为图像。首次实际识别会下载检测、方向分类和识别模型，后续复用本地文件；简历文字与页面图像不上传。默认模型目录为所配置 `data/` 下的 `ocr-models/`，可通过 `INTERVIEW_OCR_MODEL_DIR` 指定可写目录。模型无法下载时页面给出提示，可联网重试、预先准备模型目录或手工补充文字。应用启动本身不加载 OCR、不下载模型。

识别结果按 PDF 原件、识别方式和提取版本缓存，记录 OCR 页码、逐页处理方式与核对提示；已有简历版本不会被重新识别覆盖。自动模式遇到 OCR 错误时返回可用的提取文字及提示，且不缓存失败结果；整份 OCR 模式失败则返回错误并保留当前编辑输入。首次识别可能较慢，页面单次等待最多 5 分钟；超时可稍后重试。PDFium 渲染和 OCR 引擎调用在进程内串行处理，识别期间不持有数据库事务。

资料接口统一在 `/materials` 下：

| 路径 | 行为 |
|---|---|
| `POST /materials/documents/pdf?file_name=...&ocr_mode=auto` | binary PDF 上传，保存原件并返回文字、OCR 页码和提示；识别方式支持 `auto/force/off`。 |
| `POST /materials/documents/{id}/extract?ocr_mode=force` | 使用已保存原件重新识别或读取对应方式缓存，返回预览，不修改简历。 |
| `POST /materials/text/format` | JSON 提交 `source_text`，返回只整理断行和空白的文本，不写数据库、不调用模型。 |
| `GET /materials/documents/{id}/file` | 下载原始 PDF。 |
| `GET/POST /materials/resumes` | 按名称搜索、分页列出或创建简历。 |
| `GET/PUT/DELETE /materials/resumes/{id}` | 读取、保存完整内容或逻辑删除。 |
| `GET /materials/resumes/{id}/versions` | 分页列出简历版本。 |
| `GET /materials/resumes/{id}/versions/{version}` | 读取指定版本文字、来源文件和哈希。 |
| `GET/POST /materials/resumes/{id}/projects` | 分页列出或创建项目及事实。 |
| `GET/PUT/DELETE /materials/resumes/{id}/projects/{project_id}` | 读取、保存项目与事实完整内容或逻辑删除。 |

资料的 `PUT` 接口提交完整可编辑内容和 `expected_revision`；项目来源版本创建后不可改写，事实随项目在同一事务中保存。`POST` 创建可带 UUID `Idempotency-Key`，同一标识与内容重复请求返回已有记录（HTTP 200）；不同内容或已删除记录返回 409。删除通过查询参数提交 `expected_revision`。详细结构以 OpenAPI 为准。

## Qwen 提取项目与事实

在本地 `.env` 配置（密钥不发到聊天中，也不要提交仓库）：

```dotenv
INTERVIEW_QWEN_API_KEY=你的百炼APIKey
INTERVIEW_QWEN_MODEL=qwen-plus
INTERVIEW_QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
INTERVIEW_QWEN_ENABLE_THINKING=false
INTERVIEW_QWEN_TIMEOUT_SECONDS=120
INTERVIEW_QWEN_MAX_OUTPUT_TOKENS=8192
INTERVIEW_QWEN_INTERVIEW_TEMPERATURE=0.8
```

`qwen-plus` 是可替换的配置示例，不锁定最终型号。接口使用 Chat Completions 的 JSON Object 模式，并由 Pydantic 校验实际结构；所选模型需支持对应 JSON/思考模式组合。默认接口地址是北京地域现有域名，也可改为百炼控制台的业务空间专属地址；密钥和接口需同地域。[百炼兼容接口说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)、[结构化输出说明](https://help.aliyun.com/zh/model-studio/qwen-structured-output)。

重启后进入「简历与项目 → 项目与事实」：

1. 先保存并核对简历文字，再点击「AI 提取项目」。仅此操作发送已保存版本文字到所配置的 Qwen 接口，不上传原始 PDF；最多处理 60000 字符，超出时提示精简，不静默截断。
2. 提取结果保存为待确认草稿，显示候选项目、事实原文引用、模型自评置信度、需确认事项和调用用量。没有可匹配引用的项目或事实会被排除并提示；引用匹配只证明来源存在，仍需人工判断内容是否被正确概括。
3. 对候选点击「载入编辑」，在已有项目编辑区修正名称、职责、时间、技术栈、说明与事实。未知字段留空，不根据技术名词自动补写实现细节，也不替用户修复简历缺失句子。
4. 勾选「我已核对」后点击「确认并保存项目」。每个候选独立确认，在一个事务中保存项目、事实与确认映射；重复提交相同内容读取已保存项目，不重复创建。已保存候选需从项目列表修改，删除后不会因重试重新创建。
5. 提取草稿持久化，可刷新结果、切换历史提取记录及分页查看；刷新/读取记录不调用模型。新提取记录不会覆盖原简历或已有项目，不同提取任务之间不做语义去重。

超时或网络错误时页面保留请求标识；点击「读取本次结果」复用该标识，不发起第二次调用。已明确失败的提取不会自动重试，手动点击「AI 提取项目」才创建新任务和新模型调用。因重启等原因遗留的运行中任务在超时宽限期后标为失败，不自动续跑。已有草稿和人工项目管理不依赖模型密钥。

每次任务记录模型、prompt/schema 版本、状态、耗时、服务端返回的输入/输出 token 用量及脱敏错误码；不存储无效模型原始响应，不记录密钥、请求正文或简历到日志。提取和确认均固定引用具体简历版本，后续修改简历不重绑依据。运行期间的 JSON 与来源校验属于业务逻辑；本轮未运行测试、浏览器验证，也未实际调用 Qwen。

新增接口（更新 Apifox 的 `/openapi.json` 导入）：

| 路径 | 行为 |
|---|---|
| `GET /ai/status` | 只读取本地配置状态与模型名，不发网络请求，不返回密钥。 |
| `POST /materials/resumes/{id}/parse-drafts` | 提交 `request_id` UUID 和 `source_version`，提取或重放已有任务。返回草稿；模型调用失败体现在 `status/error_code/error_message`，不创建项目。 |
| `GET /materials/resumes/{id}/parse-drafts` | 分页读取提取历史，`limit` 最多 20。 |
| `GET /materials/resumes/{id}/parse-drafts/{draft_id}` | 读取任务结果、revision 和已确认候选映射。 |
| `POST /materials/resumes/{id}/parse-drafts/{draft_id}/confirm` | 提交候选序号、`expected_revision`、完整可编辑项目字段和 `user_verified=true`，确认一个项目。 |

首次使用只需按原命令重启服务，local SQLite 自动升级到当前迁移头（见「配置与数据库」）；显式 MySQL 需手动 `alembic upgrade head`。网络调用复用现有 `httpx2` 包，已将其声明移到运行依赖，不引入供应商 SDK。

## 面试准备与 Planner

打开 <http://127.0.0.1:8000/planner> 或点击侧栏「面试计划」。本轮只做面试前的规划闭环，不启动实时面试、不做逐轮分析与复盘。

1. 选择简历与具体简历版本，勾选该版本中已核对的项目（最多 8 个）和启用题库中的题目（最多 40 道）。启用题库**默认从全部启用题目中随机抽样 40 道**（不再只取最近 200 道），可点「随机重选 40 道」重新抽取或逐题调整。题目列表按导入批次时间与候选序号还原笔记原顺序；随机只决定「选哪些」，提交时按页面列表顺序固化，不改变题目先后。未核对、版本不匹配、已停用或已删除的资料会被排除并给出提示。
2. 填写考察方向（可留空）、预计时长（10–180 分钟）、项目题时间比例与追问总上限（0 为自动）。目标时长只是节奏参考，到时不强制结束。
3. 选择生成方式后点击「生成计划」。模型模式复用上一节的 Qwen 配置与 `ai/qwen.py` 适配器，仅发送已核对项目事实与启用题干，不发送简历全文、标准答案或复习笔记；规则模式完全在本地按资料分块生成，不调用模型，适合核对流程或密钥未就绪时使用。两种模式都会先确定「哪些来源参与、每类分几个目标」，再分配预算；比例为 0 的来源不会留下零预算目标。
4. 计划输出考察目标（主题、来源、优先级、可观察证据、完成规则、事实与题目引用）、题源时间预算、开场与收尾缓冲、追问总上限与每目标上限，以及分区提示。**不输出固定问题清单，也不生成任何具体问题文本**。摘要、比例换算、时间分配、追问配额、目标编号、覆盖率、候选范围与引用校验全部由服务端程序生成，模型只返回语义骨架。
5. 结果区按来源分区显示「系统校验与统计」「模型提示 · 未经核验」「产品规则」；含与所选简历版本不符的版本号的模型提示会被剔除。每个目标可展开核对引用的事实原文与启用题干，并查看预编译的运行时上下文；覆盖率卡片显示目标依据的覆盖比例，候选范围卡片显示本场可以动态选用的资料池。
6. 可查看历史计划、调整输入后重新生成（每次生成是一条新记录，不覆盖旧计划）、确认保存或逻辑删除。**确认前复核固定引用**：简历版本哈希、项目 `revision`、事实内容/类型/来源依据与题目内容版本任一变化，或资料被停用、取消核对、删除，都会返回 409 并要求重新生成。
7. 页面会区分「配置已改」与「计划可确认」：调整时长、比例、资料或题目后，旧结果的确认按钮会被禁用并提示重新生成；生成期间输入被锁定，避免结果与界面配置不一致。「读取本次结果」只读取原任务，未结束时保留入口，不重复调用模型，也不自动重试收费请求。

### 规划质量与实时链路约束

Planner 决定后续提问水平，也影响语音对答的反应速度，因此按以下原则收敛：

- **模型职责最小化**：发给模型的只有项目事实摘要（每条至多 120 字）与启用题干（每道至多 80 字），不含简历全文、标准答案或笔记；模型只输出主题、来源、优先级、引用 ID、最多 3 条能力维度与简短完成规则，不输出摘要、时间、预算或具体技术细节，也不复述技术路径、指标与版本号。
- **原文保真与消费层分离**：Markdown 导入规则、来源全文与题库内容版本一律不改；`planner/text.py` 只在组装模型输入与 `runtime_context` 时做逐行保序清理（转义反斜杠、强调标记、行首编号、`【标准回答话术】` 类标签、多余空行），不重排行、不删非空行、不合并段落，也不写回题库。项目事实保留逐字原文以维持证据可核验；计划快照按提交顺序输出题目，不再按创建时间重排。
- **格式容错、语义严格**：空白、长度、数量与未知字段在校验前自动裁剪，避免一条多余证据让整份计划失败；但 `schema_version`、来源枚举、优先级、引用 ID 与资料范围严格校验，不合法直接失败并给出可操作提示，不把非法输出包装成合法结果。
- **候选范围与目标依据分开**：计划保存 `candidate_scope`（本场允许动态选用的全部事实与题目），目标的 `project_fact_ids` / `question_ids` 只是该目标的主要依据。覆盖率表示目标引用情况，不承诺整场问完，也不为凑覆盖率强行加题。
- **提示分层**：模型自评一律标注「未经核验」，不与系统校验结果、产品规则混排；含未核验版本号的条目直接剔除，目标数按预计时长程序化确定（4–8 个）。
- **追问配额公平分配**：先给每个有意愿的目标保底 1 次，再按优先级分配剩余额度，避免尾部目标全部为 0 追问。
- **结构化运行时契约**：`GET /interview-plans/{id}/runtime` 返回 `interview_runtime.v1`：目标的结构化引用 ID、可观察证据、完成规则、预算、候选范围，以及确认时冻结的完整资料快照（含题干、标准答案与备注原文）。`context` 字段只是摘要，用于选择方向；提问、保存来源与复盘必须按 ID 读取快照原文，不能从截短文本恢复原题。
- **旧计划只读**：优化前生成的计划（`planner.v1`、快照缺 `revision` / 来源依据）仍可在列表与详情中查看，但确认与运行时接口返回 `plan_contract_outdated` 409，必须按当前配置重新生成；页面同时禁用其确认按钮。当前提示词版本为 `planner_prompt.v3` / `planner_rule.v3`。

计划固定引用所选简历版本，并保存生成时的项目 `revision`、事实内容与来源依据（`source_kind` / `evidence_quote`）、题目内容版本与标准答案原文，作为确认复核与运行时读取的冻结依据。local SQLite 重启后自动升级到当前迁移头；显式 MySQL 需手动 `alembic upgrade head`。本轮无新迁移、无新依赖，也未运行迁移、测试、静态检查、浏览器验证或模型试调用。

新增接口（更新 Apifox 的 `/openapi.json` 导入）：

| 路径 | 行为 |
|---|---|
| `GET /interview-plans/options?resume_id=...&source_version=...` | 只读返回可选简历、版本、已核对项目、启用题目与模型配置状态，不调用模型。 |
| `POST /interview-plans` | 提交 `request_id` UUID 与配置，主动生成一份计划；相同标识与配置重放原计划（HTTP 200），不重复调用模型。 |
| `GET /interview-plans` | 分页读取计划列表，不调用模型。 |
| `GET /interview-plans/{id}` | 读取计划、输入、资料快照、结果与确认状态。 |
| `GET /interview-plans/{id}/runtime` | 读取已确认计划的运行时契约 `interview_runtime.v1`：目标结构化引用、候选范围、预算约束与冻结资料快照，供实时/语音链路直接使用；未确认或旧版计划返回 409。 |
| `POST /interview-plans/{id}/confirm` | 提交 `expected_revision`，复核简历版本哈希、项目 revision、事实内容与题目内容版本后确认保存；不匹配返回 409 `plan_reference_stale`；重复确认幂等。 |
| `DELETE /interview-plans/{id}?expected_revision=1` | 逻辑删除计划。 |

## 文字模拟面试

打开 <http://127.0.0.1:8000/interview> 或点击侧栏「模拟面试」。本轮交付最小闭环：已确认计划 → 创建场次 → 首问 → 提交回答 → 追问或切题 → 结束，问答逐轮保存，刷新后可继续或回看。

1. 选择一份**已确认的计划**开始。只有满足当前契约（`planner.v2` 结果、`snapshot_version=2` 快照、项目 revision 与题目原文齐全）的计划才能开始；旧版计划仍会列出来但置灰，页面直接提示去「面试计划」页按当前配置重新生成并确认，不会等到点「开始」才报 409。创建场次时把 `interview_runtime.v1`（目标、预算、约束、候选范围与冻结资料快照）复制进场次；之后修改计划或资料不影响进行中的场次与历史记录，绕过页面直接调用时旧契约仍返回 `plan_contract_outdated`。
2. 创建场次时**一次批量生成基础问题骨架**并冻结到 `outline_json`（`interview_outline.v1`，每个项目目标最多 2 题，只依据该目标的项目事实）；开场与之后切题都优先取骨架，命中即零模型调用。骨架生成失败、密钥未配置或旧场次没有骨架时**自动回退**到即时生成，不阻断开始。题库目标不进骨架，仍直接使用**你自己的原题**（题干经消费层清理后展示，原文仍在冻结快照中）。发送给模型的只有该目标的事实摘要与题库题干，不含简历全文、标准答案或笔记。
3. 提交回答后：**回答先落库并提交事务，再调用模型**。模型只输出本轮判断（目标是否覆盖）、追问措辞、下一目标建议与一句观察；是否追问、切换目标、跳过与收尾由服务端按计划配额决定，模型建议只作为参考。一次回答最多一次模型调用：判断与措辞同一次返回，追问或下一问在同一事务里立即写入，不会出现"判定要追问却没有下一题"。
4. 追问受两个上限约束：计划的总追问上限与单目标 `max_followups`；用尽后直接切题，不会因为模型想追问而突破。追问只在回答里有可追问的内容时发生：回答短于 8 字（如"不知道"）时不追问细节，改为换该目标更基础的方面或直接切题。每个项目目标最多 2 个基础问题，用完即结束该目标；题库目标按本场的取题顺序逐题推进。目标推进顺序按优先级分层、同层内按场次打乱，模型建议的目标不在剩余目标内时回退到该顺序。
5. 计划时长只是节奏参考：超过时长 5 分钟后，在**当前回答处理完**时自然收尾，不打断正在作答的一轮；剩余时间不足 60 秒时不再新增追问。
6. 支持暂停 / 继续 / 跳过当前目标 / 结束。暂停或结束时若模型结果仍在路上，写回前会重新加锁检查场次状态与轮次序号，过期结果直接丢弃，已保存的回答与进度不受影响。
7. 模型失败（超时、无效 JSON、未配置密钥）不会丢回答：场次保留错误码与「待决策」标记，点「重试下一步」只重跑这一步，不重复保存回答，也不自动重试收费请求。提交回答与创建场次都带幂等键，重复提交由唯一约束拦截，不会产生第二条回答。
8. 规则模式完全本地运行：项目题按事实模板生成，追问按回答长度判断，用于无密钥核对闭环。规则模式不做语义判断，提问质量明显低于模型模式。
9. 同一份计划重复练习时每场问题不同：目标顺序（同优先级内）、题库取题顺序、项目事实顺序都由**场次 ID 派生**的顺序决定，同场次内稳定可复现，不同场次不同；题库题还会按同计划历史场次的出现次数排序，没问过的排前面。模型模式的判定与出题温度由 `INTERVIEW_QWEN_INTERVIEW_TEMPERATURE`（默认 0.8）控制，并把本场已问题目与同计划历史场次的近期问题一起发过去，明确要求换角度、不重复。考察目标本身不随机——每场覆盖的目标集合与完成标准仍来自冻结计划。
10. 骨架只覆盖**基础问题**，追问仍然即时生成：追问的输入是"这一轮回答里的缺口"，回答之前不存在。因此每轮判定仍是一次模型调用；骨架省掉的是开场、跳过、重试与切到无模型建议目标时的额外出题调用。骨架缺失或与当前轮次对不上时按顺序回退，不会因为骨架异常而卡住场次。

「回答先保存再调用模型、请求防重复、写回前检查场次状态」这三条是本轮的设计前提，后续加 SSE、断线重连与后台分析时不应绕过。

新增接口（更新 Apifox 的 `/openapi.json` 导入）：

| 路径 | 行为 |
|---|---|
| `GET /interviews/options` | 只读返回已确认计划与模型配置状态，不调用模型；每份计划带 `usable` 与不可用原因 `reason`，服务端已按当前契约判定，可用计划排在前面。 |
| `POST /interviews` | 提交 `request_id`、`plan_id` 与 `mode`，创建场次并生成首问；相同标识与配置重放原场次（HTTP 200），不重复调用模型。 |
| `GET /interviews` | 分页读取场次摘要（状态、轮数、目标进度），不加载完整资料快照。 |
| `GET /interviews/{id}` | 读取场次、目标进度、全部轮次与当前问题。 |
| `POST /interviews/{id}/answers` | 提交 `request_id`、`turn_id` 与 `answer_text`；回答先落库，再生成追问或下一题。 |
| `POST /interviews/{id}/retry` | 续接未完成的一步：补首问或重跑失败的决策，不重复保存回答。 |
| `POST /interviews/{id}/pause`、`POST /interviews/{id}/resume` | 提交 `expected_revision`，暂停或继续。 |
| `POST /interviews/{id}/skip` | 提交 `expected_revision`，跳过当前目标并生成下一问。 |
| `POST /interviews/{id}/end` | 提交 `expected_revision`，结束场次并保留全部问答与目标进度。 |

已知边界：本轮不提供流式输出（一次请求最长等待模型超时）、不提供逐轮后台分析、复盘、记忆候选，也不提供删除场次；暂停时长计入已用时间。面试侧的标准答案仅保留在快照与会话中，当前不发送给模型。升级前生成的 `planner.v1` 计划（没有 `candidate_scope`、项目 revision、事实证据原文与题目原文）不能用于面试，也无法就地升级：只能按当前配置重新生成并确认，旧计划保留供回看。

## 使用 Apifox 调试

简历与项目 API 也包含在同一份 OpenAPI 定义中，更新导入即可查看。PDF 上传接口 `POST /materials/documents/pdf?file_name=简历.pdf` 使用 **binary 请求体**，Content-Type 为 `application/pdf`，不使用 multipart，也不填写本机路径。

日常接口查看与调试使用 Apifox。先启动本地后端，再按以下步骤操作：

1. 在 Apifox 中新建项目，进入「项目设置 → 导入数据」，选择 OpenAPI / Swagger 的 URL 导入。
2. 填入 `http://127.0.0.1:8000/openapi.json` 并导入。这是接口定义地址，`/docs` 是 Swagger 页面，不用于 URL 导入。
3. 新建或选择「本地开发」环境，将前置 URL 设置为 `http://127.0.0.1:8000`，调试时选中此环境。
4. 先发送 `GET /health/ready`，返回 HTTP 200 后即可调试题库接口。当前接口不需要认证。

创建题目时，选择 `POST /question-bank/questions`，请求 Body 使用 JSON，示例见下文。创建成功返回题目的 `id` 和 `revision`；编辑时将 `id` 填入路径参数，将最新 `revision` 作为请求体中的 `expected_revision`，例如：

```json
{
  "expected_revision": 1,
  "user_notes": "补充自己的复习笔记"
}
```

接口新增或调整后，重新导入同一 OpenAPI URL。接口定义以代码生成的内容为准，无需在仓库额外维护一份静态 JSON。服务未启动时无法通过本地 URL 导入或调试。操作参考 [Apifox 官方导入说明](https://docs.apifox.com/import-openapi-swagger)。

## 配置与数据库

环境变量统一使用 `INTERVIEW_` 前缀；同名环境变量优先于 `.env`，空环境变量按未配置处理。配置项见 `.env.example`。

| 配置情况 | 行为 |
|---|---|
| `INTERVIEW_ENVIRONMENT=local`，连接串留空 | 默认 SQLite，启动时自动运行迁移。 |
| 显式设置 `INTERVIEW_DATABASE_URL` | 使用指定数据库；仅 local 环境的 SQLite 自动迁移。 |
| `production` 或 `test` 环境，连接串留空 | 保持未配置状态，不自动回退到本地文件。 |

可通过 `INTERVIEW_DATA_DIR` 调整默认文件目录；相对路径按启动时的工作目录解析。SQLite 外键检查已启用，使用与 MySQL 相同的内容版本和元数据关系。备份本地数据时，停止服务后复制 `data/` 即可。

需要连接 MySQL 时，在本机另行创建数据库并配置：

```dotenv
INTERVIEW_DATABASE_URL=mysql+aiomysql://interview:password@127.0.0.1:3306/interview?charset=utf8mb4
```

用户名和密码中的特殊字符需要 URL 编码。先单独创建使用 `utf8mb4` 的 MySQL 数据库，再显式执行：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

迁移包含题库的 `question`、`question_version`、`question_metadata`、`markdown_import`、`question_import_source`，资料管理的 `resume_document`、`resume_extraction`、`resume_parse_draft`、`resume`、`resume_version`、`project`、`project_fact`，面试计划的 `interview_plan`，以及文字面试的 `interview_session`、`interview_turn`（当前 head 为 `0008_interview_outline`，为场次增加基础问题骨架 `outline_json`）。现有本地 SQLite 在下次启动时自动升级，已有题目与版本保留。MySQL 不在应用启动时自动建库、建表或运行迁移，更新后仍需手动执行迁移命令。可用 `python -m alembic current` 查看当前数据库版本；`python -m alembic upgrade head --sql` 按当前配置离线生成 SQL，`python -m alembic -x dialect=mysql upgrade head --sql` 可在未配置 MySQL 时预览它的 SQL。

`.env`、虚拟环境与 `data/` 不进入版本控制。请勿将简历、题库、数据库凭据或模型密钥加入源码仓库。

Ruff、mypy 和 pytest 的缓存统一放在已忽略的 `.cache/` 中，可随时删除；`.venv/` 是本地运行环境，日常清理保留。项目协作规则见 [AGENTS.md](AGENTS.md)。

## 代码组织

```text
src/interview_assistant/
  main.py             应用工厂、启动和资源释放
  config.py           配置验证
  logging_config.py   应用日志
  database.py         SQLite/MySQL 引擎、会话工厂与本地初始化
  api/
    dependencies.py   请求级依赖与数据库会话
    health.py         存活和就绪检查
    sse.py            SSE 编码和响应工具
  ai/
    protocol.py       与供应商无关的结构化输出协议
    qwen.py           Qwen 结构化调用、超时、用量与脱敏错误
    router.py         本地模型配置状态接口
  question_bank/      题库模型、Markdown 提取与导入事务、请求/响应结构及 API
  materials/          PDF/OCR、简历版本、项目事实、AI 提取草稿与人工确认 API
  planner/            面试计划模型、结构化输出契约、生成与确认服务及 API
  interview/          文字面试场次与轮次模型、决策契约、运行时服务及 API
  web/                题库、导入预览、简历与项目、面试计划、模拟面试页面，同源调用现有 API
migrations/           Alembic 迁移环境和版本脚本
data/                 本地 SQLite 数据，已忽略版本控制
tests/                使用临时文件数据库的离线检查
```

后续逐步加入题库元数据写入、流式事件与断线重连、逐轮后台分析、复盘与记忆候选，并同步补充对应页面。当前先用轻量原生页面完成闭环；是否引入前端框架根据后续交互复杂度决定，部署方案仍待选择。

## 题库 API

基础路径为 `/question-bank/questions`，完整请求结构导入 Apifox 后查看；`/docs` 保留为备用入口：

| 方法/路径 | 行为 |
|---|---|
| `POST /question-bank/questions` | 创建题目，保存第一份内容快照。 |
| `GET /question-bank/questions` | 分页查询；支持 `is_active` 和题干字面文本 `search` 筛选。 |
| `GET /question-bank/questions/{id}` | 读取当前内容、`revision` 和 `content_version`。 |
| `PATCH /question-bank/questions/{id}` | 编辑题干、答案、备注或启停；必须提供 `expected_revision`。 |
| `DELETE /question-bank/questions/{id}?expected_revision=1` | 逻辑删除，保留历史和关联元数据；普通接口不再返回该题。 |
| `GET /question-bank/questions/{id}/versions` | 分页查看内容版本，新版本在前。 |
| `GET /question-bank/questions/{id}/versions/{version}` | 读取指定版本原文。 |

创建示例：

```json
{
  "question_text": "事务的原子性是什么？",
  "standard_answer_text": "用户自己的答案，可包含 Markdown 和代码块。",
  "user_notes": "需要复习的内容",
  "is_active": true
}
```

题干不能全为空白；答案和备注允许空字符串，内容不做自动裁剪、换行转换或润色。编辑时省略的字段保持原值，显式 `null` 会被拒绝。内容实际变化才增加 `content_version` 并原子保存快照；启停只更新 `revision`。旧 `expected_revision` 返回 HTTP 409，避免覆盖新修改。

创建请求可携带 `Idempotency-Key`（1–128 个英文字母、数字或 `_.:-`）。同一标识和同一请求只创建一次，重试返回 HTTP 200 和该题当前状态；标识用于不同内容或对应题目已删除时返回 HTTP 409。未提供标识的创建请求独立处理。元数据绑定具体内容版本，本阶段不提供 AI 元数据写入接口。

Markdown 接口同样可通过 `/openapi.json` 导入 Apifox：

| 方法/路径 | 行为 |
|---|---|
| `POST /question-bank/imports` | JSON 提交 `file_name`、原文 `content` 和可选 `strategy`，创建预览草稿（201）或复用已有批次（200）。 |
| `GET /question-bank/imports/{id}` | 读取候选题、提示与批次确认状态。 |
| `POST /question-bank/imports/{id}/confirm` | 提交 `selected_indices` 数组，以预览中的 `candidates.index` 为准；整批保存或安全重放。 |
| `GET /question-bank/imports/{id}/document` | 下载 UTF-8 来源文件，保留 BOM 与换行。 |
| `GET /question-bank/questions/{id}/source` | 读取来源文件信息、行号与原始段落；手工题或已删除题返回 404。 |

预览 API 使用 JSON 文本而非 multipart；客户端不提交本机文件路径。确认 API 仅接收候选序号，题干、答案与来源均从服务器保存的草稿读取。

## 检查命令

按用户约定，日常开发默认不由助手主动运行测试、静态检查或浏览器验证，由用户启动服务后体验；以下命令仅供需要时手工执行。简历与项目、PDF/OCR、Qwen 提取、面试计划以及本轮新增的文字面试均尚未运行验证。

```powershell
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest
```

测试覆盖基础设施、页面与静态资源路由、SQLite 启动初始化、原文保真、重启后持久化、编辑冲突、Markdown 拆分与来源、选择导入、并发确认、失败回滚、元数据隔离和迁移升降级。使用临时 SQLite 文件运行实际迁移与接口，并阻止 MySQL 驱动连接；另检查 MySQL SQL 生成与旧版数据升级。测试不调用 Qwen，MySQL 联调仍需在独立开发数据库中进行。CI 配置不代表已在远端运行。页面交互需另用浏览器验证，Python 测试不代替浏览器检查。

## 后续讨论已确认的补充

以下是设计文档之后的用户确认，后续实现应遵循这些约束：

- 通用题使用用户原题，允许动态追问；追问只保存到场次，不自动写回题库。
- 目标时长是规划参考，到时允许继续，不强制结束；通过目标范围和有限追问避免无限提问。具体次数默认值尚未确定。
- 详细反馈放在面试后，首版不显示综合总分；长期记忆候选经用户确认后才影响后续计划。
- 题库主要来自 Markdown Q-A 笔记；图片暂不识别，AI 不补写题干或标准答案。
- 模型优先 Qwen 系列，具体型号与接入参数后续确定。

Markdown 首版切分与预览行为见上文；更复杂的笔记格式后续按实际文件补充。设计稿中“时长是硬边界”“时间到结束”的旧表述后续需按上述确认同步。

## 本轮交接

- **本轮完成项**：基础问题骨架 + 场次多样性。新增 `interview_outline.v1`（`schemas.py` 的 `OutlineDraft` / `SessionOutline`）与 `interview/variety.py`（场次 ID 派生顺序）；迁移 `0008_interview_outline` 给 `interview_session` 加 `outline_json`。创建场次时一次批量生成每个项目目标最多 2 个基础问题并冻结，`_ask_next` / `_next_turn` 优先取骨架、缺失回退即时生成，骨架失败只降级不阻断创建；规则模式用同一套事实模板生成骨架。目标顺序（同优先级内）、题库取题顺序、项目事实顺序按 `sha256(session_id)` 打乱并跨场优先未问过的题；提示词升到 `interview_prompt.v3`（`asked_questions` / `avoid_recent_questions`），新增 `interview_outline_prompt.v1`；面试调用单独用 `INTERVIEW_QWEN_INTERVIEW_TEMPERATURE`（默认 0.8），资料抽取不受影响。
- **遗留问题**：按约定未运行迁移、测试、静态检查、浏览器验证或模型试调用，需用户启动服务体验。创建场次现在要等一次批量调用（输出更长，预计比单轮 6–7 秒更久），换取开场与切题零额外出题调用；每轮判定调用仍在，所以单轮等待没有本质变化，要压到 3 秒内必须做流式。骨架是一次性产物，之后修改计划或资料不会更新它（与 `runtime_json` 的冻结语义一致）；旧场次 `outline_json` 为 NULL，全部走即时生成。本地库里 4 份历史计划全部是 `planner.v1`（2 份已确认），面试页仍需先重新生成并确认一份。面试无 SSE 流式与断线重连，一次请求最长等待模型超时；暂停时长计入已用时间；暂不支持删除场次；标准答案只保留在快照中、未发送给模型。`interview/`、`planner/`、`materials/`、`ai/` 均无行为测试；目标文本与引用事实之间仍只校验 ID 有效性。
- **下一步**：先跑一场确认骨架写入与取用（`outline_json` 有值、切题不再多一次调用、失败时能回退），再补 SSE 流式输出与断线恢复，把单轮等待压到 3 秒以内；之后做整场复盘（逐轮观察已随决策写入 `interview_turn.decision_json`）。题库 AI 标签分类优先级更低；语音、复杂能力模型、知识图谱、多用户与部署扩展继续后置。每轮交付后按同样三段格式更新本节，避免重新梳理整个项目。
