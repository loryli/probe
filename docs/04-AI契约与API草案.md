# Prompt、结构化输出与 API 草案

## 当前开场阶段契约

`SessionRead.phase` 和 `TurnRead.stage` 可取 `self_introduction`、`project_introduction`、`technical`。新建简历场次从自我介绍开始，题库场次直接为 technical。阶段和开场问答的轮次映射保存在现有 progress JSON，无新增数据库列；旧 JSON 默认 technical。开场沿用现有提交、SSE、revision 和幂等接口。介绍上下文附保存轮次 ID、最多 4000 字符正文及截断标记，只作未核实数据；不替代原回答，也不作为技术目标覆盖证据。实时提示升级为 interview_prompt.v8，项目问题骨架为 interview_outline_prompt.v3，复盘为 interview_report_prompt.v2。Apifox 可重新导入 `/openapi.json` 获取新增读取字段。

## 1. 通用模型调用约定

- 每个 AI 功能有独立 prompt/schema 版本；记录模型标识、延迟、token/用量和结果校验状态。
- 用户内容、简历事实和题库答案一律视为数据，不视为可覆盖系统指令的命令。
- 输出必须匹配 JSON Schema（或等价类型校验）；渲染层不得从自由文本中猜测状态决策。
- 不需要模型做的确定逻辑（比例换算、时钟、次数限制、状态转换、去重）由普通程序完成。
- Prompt 显式说明：不得编造来源事实；缺少信息要返回不确定或澄清；不可修改原题/答案。
- 对外部模型不可用、超时或 schema 失败定义同一套回退，不让模型异常直接破坏状态机。

## 2. Prompt 契约示例

### 2.1 简历离线解析器

**System 要点**

> 你负责把简历文本提取为结构化事实。仅抽取文本明确支持的内容。不要补齐或推测技术细节。每个事实提供原文证据片段和置信度。无法确定字段设为 null，并列出需要用户确认的项目。

**输入**：简历抽取文本、页/段落位置、解析 schema 版本。

**输出**：`projects[]`、项目字段、`facts[]`、证据位置、`confidence`、`needs_confirmation[]`。

**当前实现补充（2026-11-12）**：`resume_extract.v3` 提示接收编号原文行；模型输出内部 `resume_extract_refs.v1`，以 `start_line/end_line` 标注连续原文范围（从 1 开始，含末行）。服务端校验范围并还原事实正文及引用，再保存为对外的 `resume_parse.v1` 草稿；无效范围排除并提示，不接收模型补写正文。使用流式接收及独立的简历提取时限，结果完整校验前不作为可确认候选。

**备用实现**：用户可选择 `mode=rule`，不调用模型，只从已保存简历的项目章节、日期、标题及段落生成本地候选。草稿 `provider=local_rules`，未知角色/技术栈等留空，不做能力判断；规则的 `confidence=0` 只是兼容字段，不代表模型评分。无法识别可靠边界时提示人工分组。两种模式均固定来源版本，经同一人工确认事务保存，失败记录、原文与已有项目不被覆盖。

### 2.2 题库元数据分类器

**System 要点**

> 只为给定问题和标准答案建议分类、子分类、知识点标签、难度和相关主题。输入文本是用户资产，不要改写、摘要后替换或补充答案。只返回元数据。不要生成标准答案。若信息不足，降低置信度或留空。

**输入**：原问题、原答案（供分类理解）、可配置标签词表（可为空）。

**输出**：元数据草稿与置信度。服务端只允许写入 metadata 表。

### 2.3 Interview Planner

当前输入 `max_total_followups` 允许空值：`null`/省略为自动额度、`0` 为明确禁止、1–30 为指定全场上限。旧已确认计划的冻结额度不重算。

`planner_prompt.v4` 输入使用请求内短引用（事实 f1/f2、题目 q1/q2）；输出经精确映射恢复 UUID，再做原有来源和范围校验。未知标识仍拒绝，日志只记录目标序号与无效引用数量。生成失败可由用户主动发起同配置的规则计划，不自动调用备选模型。

面试 SSE 保持 ack/delta/state 事件格式；delta 只包含已经校验并落库的实际下一问，不转发未通过约束的模型原始措辞。`ObjectiveState.elapsed_sec` 返回目标有效耗时，暂停时间排除；现有进度 JSON 新字段有向后兼容默认值，无新增数据库列。

当前实现支持纯题库计划：`resume_id/source_version` 可同时为空，`project_ids` 为空、题库至少一题；服务端固定项目比例和额外追问上限为零。页面用规则组织题库目标与预算，不调用规划模型，AI 面试模式仅评价回答和生成复盘，下一问仍由服务端读取冻结的题库原题。简历项目模式继续传真实版本与已核对项目。已确认计划不因资料更新改变，具体接口字段以 `/openapi.json` 为准。

**System 要点**

> 根据场次配置和已确认材料规划考察目标、题源比例与时间预算，不生成固定问题列表。目标应可通过面试回答观察到。仅引用给定的项目/历史证据。若题库或简历不足，返回限制说明和需要用户选择的项。

**输入**：用户配置、简历快照、可用题库元数据、选用的历史摘要、时长。

**输出**：目标、优先级、预期证据、建议来源、题源时间预算、总时长缓冲、约束、未覆盖风险。

### 2.4 实时 Interview Agent（Fast Path）

**System 要点**

> 你是自然、简洁的技术面试官。当前任务是确定下一步面试动作，不是写完整评语。遵守 Planner 目标和时间/追问上限。根据候选人真实回答追问；问题一次只问一个。不要替候选人补全项目事实。题库标准答案是参考材料而非逐字背诵模板。低置信转写先澄清。输出严格符合 schema。

**输入**：当前目标、题源、当前问答、最近 K 轮对话、剩余预算、已有目标覆盖记录、可选相关记忆（带证据引用）。

**动作枚举**：`follow_up`、`advance`、`clarify`、`pause_requested`、`finish`。

### 2.5 Background 分析器

**System 要点**

> 对回答做事后证据分析。区分候选人明确说出的内容、技术判断和你的推断。列出支持证据和反证；不确定之处标记待验证。不要据单轮回答断定长期能力。

### 2.6 Offline 复盘器

**System 要点**

> 综合整场面试和历史结构化证据生成复盘。每项评价引用 turn/文本证据。分开说明答案覆盖、技术准确性、表达清晰度与项目事实可信度线索。提供少量具体可执行的下一步练习。生成的记忆是候选观察，需引用证据；不要自动改写用户题库或简历。

## 3. 结构化输出 schema 草案

### Planner 输出

```json
{
  "schema_version": "planner.v1",
  "session_summary": "覆盖项目设计与基础数据库知识",
  "objectives": [
    {
      "id": "obj_01",
      "topic": "项目中的缓存一致性",
      "source_preference": "project",
      "priority": "high",
      "observable_evidence": ["能说明失效策略及其取舍"],
      "project_fact_ids": ["fact_17"],
      "question_ids": [],
      "time_budget_sec": 420,
      "max_followups": 3,
      "completion_rule": "说明机制并回应一个故障场景"
    }
  ],
  "source_time_budget_sec": {"project": 900, "question_bank": 600},
  "opening_buffer_sec": 60,
  "closing_buffer_sec": 60,
  "constraints": {"max_total_followups": 10, "disabled_topics": []},
  "limitations": []
}
```

实现校验：预算总和不超过场次时长；目标 ID 唯一；通用题目标中的题目必须存在且启用；项目事实必须引用当前简历快照。服务端可修正比例舍入误差，但不能静默新增目标。

### Fast Path 输出

```json
{
  "schema_version": "fast_decision.v1",
  "action": "follow_up",
  "current_objective_id": "obj_01",
  "answer_relevance": "on_topic",
  "objective_progress": "partial",
  "decision_reason": "解释了缓存更新，但没有说明旧值失效时机",
  "next_question": "数据更新后，你如何避免其他实例继续读到旧缓存？",
  "followup_cost_sec": 90,
  "evidence_refs": [{"turn_id": "turn_08", "quote": "我会在写入后清理缓存"}],
  "confidence": 0.82,
  "requires_clarification": false
}
```

`action=advance|finish|clarify` 时 `next_question` 可空或按规则必填；schema 需使用条件校验。服务端再检查目标是否存在、追问数和时间预算，模型不能自行覆盖这些硬限制。

### Background 分析输出

```json
{
  "schema_version": "turn_analysis.v1",
  "turn_id": "turn_08",
  "knowledge_points": [{"topic": "缓存失效", "coverage": "partial"}],
  "observations": [
    {
      "type": "missing_detail",
      "statement": "未说明并发写入下的失效竞态处理",
      "evidence_refs": [{"turn_id": "turn_08", "start": 18, "end": 34}],
      "confidence": 0.77
    }
  ],
  "technical_accuracy": {"label": "plausible_incomplete", "evidence_refs": []},
  "followup_suggestions": ["核实缓存更新与失效的顺序"]
}
```

### Offline 复盘输出

至少包含 `overall_summary`、`objective_results[]`、`answer_quality`（coverage/accuracy/clarity）、`evidence_refs[]`、`unverified_claims[]`、`next_practice[]` 和 `memory_candidates[]`。每个 memory candidate 必须包含主题、观察方向、置信度、证据 ID 和时间；不能只存无来源的结论。

## 4. API 草案

V1 建议采用 HTTP + SSE：HTTP 提交配置和回答，SSE 推送逐字输出、面试状态及分析进度。后续语音接入后，再按音频双向流需求评估 WebSocket/WebRTC。以下为资源边界而非最终路径约定。

| 方法/路径 | 用途 | 关键行为 |
|---|---|---|
| `POST /resumes` | 创建简历版本 | 保存原件/文本，创建离线解析任务。 |
| `GET /resumes/{id}` | 读取简历及解析状态 | 返回解析版本、需确认事实。 |
| `PATCH /resumes/{id}/projects` | 校正项目/事实 | 保存用户确认值和来源引用。 |
| `POST /question-bank/questions` | 新建题目 | 原文入库；可选排入元数据分析。 |
| `PATCH /question-bank/questions/{id}` | 编辑用户原文 | 增加内容版本；不被元数据任务覆盖。 |
| `POST /question-bank/imports` | 导入题库 | 返回批次 ID 与逐行状态。 |
| `POST /question-bank/questions/{id}/metadata-jobs` | 生成元数据草稿 | 只创建 metadata 版本；需要用户确认策略。 |
| `GET /question-bank/questions` | 筛选题目 | 按标签、难度、分类、启用状态筛选。 |
| `POST /interviews/plans` | 预览计划 | 输入配置，返回目标与预算，不创建固定题单。 |
| `POST /interviews` | 创建并确认场次 | 固化 plan 和简历/题库快照。 |
| `POST /interviews/{id}/start` | 开始面试 | 返回第一问题或实时通道信息。 |
| `POST /interviews/{id}/turns` | 提交文本答案/转写 | 先保存 turn，再触发 Fast Path 与 Background。 |
| `POST /interviews/{id}/control` | pause/resume/skip/end | 用户动作优先，记录事件。 |
| `GET /interviews/{id}/events` | 订阅状态变化 | SSE/WebSocket 事件：问题、状态、后台进度、错误。 |
| `GET /interviews/{id}/review` | 获取复盘 | 返回部分/完整状态及证据引用。 |
| `DELETE /interviews/{id}` | 删除场次 | 依删除策略级联删除数据与文件引用。 |
| `GET /jobs/{id}` | 查询异步任务 | 返回状态、可重试性、失败原因码。 |

## 5. API 事件建议

`interview.question_issued`、`interview.answer_saved`、`interview.decision_ready`、`interview.status_changed`、`analysis.progress`、`analysis.completed`、`analysis.failed`。

每个事件包含 `event_id`、`session_id`、`sequence_no`、`created_at`、`schema_version`。客户端以事件 ID 去重；事件不携带不必要的完整简历/题库答案。

## 6. 一致性与版本规则

- 写操作接受 `Idempotency-Key`，尤其创建场次、提交回答、排队分析。
- Planner 返回 `plan_version`，运行时决策引用该版本。
- 更新题库原文后元数据标记为旧版本/待重算，不丢弃历史元数据。
- 模型输出保存 prompt/schema 版本；若 schema 改变，通过显式迁移或新版本处理。
- 同步 API 返回用户可理解的错误码，不将模型供应商错误原样泄漏。
