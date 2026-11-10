# 练习场关键用户流程

日期：2026-11-10。状态：待确认交互方案，不是已实现流程声明。下图省略底层实现细节，但保留读取、保存、生成和确认的区别。对应细节见 [总体架构](UX_ARCHITECTURE.md) 与四页 UX 文档。

## 1. 总体训练路径与返回路径

```mermaid
flowchart TD
    Entry[进入练习场四个工作区] --> Q[题库：维护自己的问答]
    Entry --> M[资料：核对简历和项目]
    Entry --> P[计划目录]
    Entry --> History[面试记录：继续或回看]
    Q --> P
    M --> P
    P --> Config[分步配置并生成目标]
    Config --> Preview[预览计划]
    Preview --> Confirm[确认计划]
    Confirm --> Start[开始页：选择出题方式]
    Start --> Run[明确点击开始面试]
    History --> Run
    Run --> Record[问答保存与场次结束]
    Record --> Report[主动生成或读取复盘]
    Report -. 自主修改资料 .-> M
    Report -. 自主整理知识 .-> Q
    Report -. 再次规划 .-> P
```

已有计划可直接进入开始页；已有场次可继续，不强制重新走准备流程。读取历史与生成复盘不同。最后三条路径不自动改内容，也不预填未实现的弱项记忆。

## 2. 阅读、编辑与并发冲突

适用题库、简历和项目；计划调整只生成新计划，不覆盖既有计划。

```mermaid
flowchart TD
    List[列表选择对象] --> Read[读取已保存详情]
    Read --> Edit[显式进入编辑]
    Edit --> Save[点击保存]
    Save --> Result{服务端结果}
    Result -- 成功 --> Read
    Result -- 字段错误 --> Correct[字段旁提示并保留全部草稿]
    Correct --> Edit
    Result -- 版本冲突 --> Conflict[草稿保留并说明对象变化]
    Conflict --> Copy[复制自己的草稿]
    Copy --> Latest[用户主动读取最新]
    Latest --> Edit
    Result -- 结果未知 --> Verify[保留输入并核实本次保存结果]
    Verify --> Read
    Verify --> Edit
    Edit --> Leave{尝试取消或切换对象}
    Leave -- 留在当前 --> Edit
    Leave -- 确认放弃 --> Read
```

“核实”不代表自动覆盖草稿或盲目重发。尚无通用合并界面，不画成自动三方合并；删除与失效场景允许复制本地草稿后返回列表。

## 3. 批量移动分类

```mermaid
flowchart LR
    A[分类或搜索结果] --> B[勾选本页题目]
    B --> C[显示数量与移动操作]
    C --> D[选择目标分类]
    D --> E[携带每题revision确认]
    E --> F{结果}
    F -- 成功 --> G[刷新计数与当前列表并清除选择]
    F -- 冲突 --> H[保留选择并提示刷新后重新核对]
    F -- 网络不明 --> I[保留本次操作上下文并读取最新状态]
```

本页选择不自动扩大成跨页全选；换页或改变条件清除选择前给予说明。移动不增加答案内容版本，不改变用户原文。

## 4. Markdown 导入

```mermaid
flowchart TD
    File[选择文件和识别方式] --> Parse[请求生成预览]
    Parse --> Check{是否可解析}
    Check -- 否 --> Fix[显示编码或格式原因并保留选择]
    Fix --> File
    Check -- 是 --> Preview[按原顺序核对题干答案及来源]
    Preview --> Pick[勾选候选]
    Pick --> Confirm[确认选中的N道]
    Confirm --> Status{确认结果}
    Status -- 成功或同选择重放 --> Done[显示导入数量并返回题库未分类]
    Status -- 不确定 --> Locked[锁定本次选择]
    Locked --> Retry[重试相同确认]
    Retry --> Status
    Status -- 已由其它选择确认 --> Read[读取批次实际确认状态]
    Read --> Preview
```

预览只保存草稿；确认才创建题目。文件/策略变化形成另一批次，应明确提醒可能重复；不自动调用模型“修复”问题或补答案。

## 5. PDF、OCR和人工资料确认

```mermaid
flowchart TD
    New[新建简历] --> Input{资料方式}
    Input -- PDF --> Original[选择原件及OCR方式]
    Original --> Extract[上传与识别请求]
    Extract --> Preview[只读原件对照和识别文字草稿]
    Input -- 文本 --> Draft[填写文字草稿]
    Preview --> Draft
    Draft --> Save[保存为简历版本]
    Save --> Project{项目维护方式}
    Project -- 手工 --> Edit[编辑项目及事实]
    Project -- AI辅助 --> Call[主动发送指定已保存版本文字]
    Call --> Task[保存可回读的提取任务]
    Task --> Candidate[选择一个候选并载入核对]
    Candidate --> Edit
    Edit --> Evidence[核对固定版来源及手工补充]
    Evidence --> Confirm[勾选核对并确认保存]
    Confirm --> Saved[正式项目与来源映射]
```

OCR失败保留已有文字，可改模式或手工输入；AI失败保留原简历，不创建项目。PDF预览是拟新增呈现，不支持的浏览器使用下载兜底。重识别返回预览，只有保存才影响简历版本。

## 6. 分步 Planner 到面试开始

```mermaid
flowchart TD
    List[计划目录] --> S1[第1步：简历版本和项目]
    S1 --> S2[第2步：题库候选范围]
    S2 --> S3[第3步：方向节奏与生成方式]
    S2 -- 返回保留选择 --> S1
    S3 -- 返回保留配置 --> S2
    S3 --> Generate[主动生成]
    Generate --> Task{任务状态}
    Task -- 运行中或超时不明 --> Read[读取同一任务]
    Read --> Task
    Task -- 明确失败 --> S3
    Task -- ready --> S4[第4步：预览目标预算和依据]
    S4 -- 修改后需重新生成 --> S3
    S4 --> Confirm[用户确认计划]
    Confirm --> Valid{确认前引用是否有效}
    Valid -- 否 --> Explain[保留旧结果并指出资料变化]
    Explain --> S1
    Valid -- 是 --> Frozen[已确认固定计划]
    Frozen --> Start[进入面试开始页并带入计划]
    Start --> Mode[选择Qwen或规则出题]
    Mode --> Create[明确点击开始才创建场次]
```

步骤1–3前进和后退不是服务端保存。确认与启动之间有独立决定，避免一次点击隐式调用两类模型。跨页带计划是前端导航建议，服务端仍检查可用性。

## 7. 面试提交、流式输出与恢复

```mermaid
sequenceDiagram
    actor U as 用户
    participant UI as 面试工作台
    participant API as 场次服务
    participant AI as 面试决策
    U->>UI: 输入并提交回答
    UI->>API: 当前问题与回答及请求标识
    API->>API: 先保存回答
    API-->>UI: ack：回答已保存
    UI-->>U: 明确保存成功，等待下一步
    API->>AI: 决策与生成
    AI-->>API: 增量文本
    API-->>UI: delta：临时显示
    API->>API: 校验并保存最终轮次/状态
    API-->>UI: state：正式快照
    UI-->>U: 显示追问/新问题并恢复输入
    Note over UI,API: 断线后读取场次快照，不依赖临时字符重放
```

恢复分支：

```mermaid
flowchart TD
    Lost[失败或断线] --> Ack{已经收到保存确认吗}
    Ack -- 否 --> Draft[保留本地回答草稿并核实]
    Ack -- 是 --> Saved[显示回答已保存]
    Draft --> Get[读取场次]
    Saved --> Get
    Get --> State{当前状态}
    State -- 已有正式问题 --> Ready[替换临时输出并继续作答]
    State -- 仍在生成 --> Wait[订阅或轮询已有任务]
    State -- 明确失败 --> Retry[用户点击继续生成]
    State -- 已暂停 --> Pause[显示继续入口]
    State -- 已结束 --> Record[查看保存记录]
    Retry --> Wait
```

不在重连时自动提交第二份回答，不把模型失败写成用户答错。流和轮询只是读取机制，不代表完成持久化事件重放。

## 8. 专注模式与辅助自由切换

```mermaid
flowchart LR
    Focus[专注：问题与回答草稿] --> Choice{展开辅助}
    Choice --> A[个人参考答案]
    Choice --> B[当前考察目标]
    Choice --> C[已有资料的知识线索]
    Choice --> D[历史问答]
    A --> Back[关闭或返回训练]
    B --> Back
    C --> Back
    D --> Back
    Back --> Focus
```

并列面板允许继续作答；覆盖面板返回时保留选区和滚动位置。切换辅助不暂停、不保存、不新增模型调用，不自动认定使用答案导致扣分。

## 9. 暂停、结束与复盘

```mermaid
flowchart TD
    Run[可操作的面试状态] --> Pause[暂停成功：保留草稿]
    Pause --> Resume[继续并读取真实状态]
    Resume --> Run
    Run --> EndAsk[结束确认：未提交草稿不保存]
    Pause --> EndAsk
    EndAsk -- 取消 --> Keep[回到原状态和草稿]
    EndAsk -- 确认 --> End[服务端确认已结束]
    End --> Record[先展示已保存问答]
    Record --> Existing{已有复盘吗}
    Existing -- 有 --> Read[读取报告]
    Existing -- 无 --> Click[用户主动生成复盘]
    Click --> Result{报告结果}
    Result -- 就绪 --> Read
    Result -- 失败 --> Retry[问答仍可看且可显式重试]
    Retry --> Click
    Read --> Evidence[查看对应轮次证据]
    Evidence --> Read
```

生成中控制按钮仍受现有可用性限制。图中不指定超时自动收尾政策，也不表示暂停时计时已从后端扣除。

## 10. 跨页离开与未保存内容

| 来源 | 已保存内容 | 未保存内容 | 离开处理 |
|---|---|---|---|
| 题库编辑 | 原题及历史 | 当前改动 | 留在当前 / 放弃并离开 |
| 资料核对 | 原件、已存版本、提取任务 | 人工修正、项目编辑 | 指出是哪份草稿；一方保存不能清空另一方 |
| 计划步骤 | 已提交任务和历史结果 | 未提交配置 | 告知步骤不是自动保存；保留当前或放弃 |
| 面试 | 已ack回答、正式轮次 | 当前回答 | 留在当前 / 确认离开；不暗示退出页面自动结束场次 |
| 复盘 | 已保存问答和已存报告 | 无普通编辑草稿 | 读取失败局部重试；离开不重新生成报告 |

浏览器刷新或关闭无法依赖本地草稿永久保存。后续如增加自动草稿持久化，必须单独设计保留范围、生命周期和数据保护，本阶段不作承诺。

## 11. 确认范围

本次交付供用户确认的是六份文档中的交互方案：四种布局、阅读/编辑层级、来源核对、分步计划、确认与启动分离、专注面试及辅助面板。待确认的五分钟收尾、生成中控制、查看答案评价和暂停计时口径，仍作为独立问题，不在本阶段改变。确认设计后再按页面进入实施；本轮不编码、不改变接口或数据库，也不运行验证。
