"""模型行号还原与本地备选提取；只产出待核对草稿，不写入项目。"""

import re

from interview_assistant.materials.parse_schemas import (
    ExtractedFact,
    ExtractedProject,
    ReferencedResumeOutput,
    ResumeParseOutput,
)

RULE_PROMPT_VERSION = "resume_local_rules.v1"
_PROJECT_SECTION = {"项目经验", "项目经历", "项目实践", "个人项目", "项目作品", "projects", "projectexperience"}
_END_SECTION = {
    "专业技能", "技术技能", "个人技能", "技能清单", "教育背景", "教育经历", "工作经历",
    "工作经验", "实习经验", "实习经历", "自我评价", "获奖情况", "荣誉奖项", "基本信息",
    "skills", "education", "workexperience", "awards",
}
_DATE = re.compile(
    r"^(?P<date>\d{4}[./年-]\d{1,2}(?:月)?\s*[-—–~～至]\s*"
    r"(?:\d{4}[./年-]\d{1,2}(?:月)?|至今|现在|present))"
    r"(?:\s+(?P<title>.+))?$", re.I,
)
_NAMED_PROJECT = re.compile(r"^(?:项目名称|项目[一二三四五六七八九十\d]+)\s*[:：、.．]\s*(.+)$")
_BULLET = re.compile(r"^\s*(?:[·•●▪*-]|\d+[、.．])\s*")
_FIELD = re.compile(r"^(?:【|\[)?(?:项目描述|项目背景|项目职责|核心职责|核心职责与成果|职责与成果|技术栈|担任角色|项目角色|角色)")


def _source_warnings(text: str) -> list[str]:
    warnings = []
    if any(text.count(left) != text.count(right) for left, right in (("「", "」"), ("『", "』"), ("【", "】"))):
        warnings.append("原文范围中有未配对的中文引号或括号，可能缺字、截断或选取范围不完整；请对照 PDF 核对，不自动补齐。")
    if "\ufffd" in text or any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        warnings.append("原文含替换字符或异常控制字符，可能存在文字提取损坏；请对照原件修正。")
    return warnings


def with_quality_warnings(output: ResumeParseOutput) -> ResumeParseOutput:
    """补充可解释的排版疑点，不纠字、不删除事实，也不把来源存在视为内容完整。

    读取历史草稿时同样适用；仅派生展示结果，不改写历史任务或 revision。
    """
    result = output.model_copy(deep=True)
    for project in result.projects:
        notes = _source_warnings(project.evidence_quote)
        # 空字段不证明原文缺失，也可能是未提取；不推断缺失原因或补写技术栈。
        if not project.tech_stack:
            notes.append("本候选技术栈为空，可对照原文核对；未明确列出的内容允许留空，也可手动补充。")
        for fact in project.facts:
            suspicious = _source_warnings(fact.evidence_quote)
            fact.needs_confirmation = list(dict.fromkeys([*suspicious, *fact.needs_confirmation]))[:20]
            if suspicious:
                notes.append(f"「{fact.fact_type}」的原文可能不完整，请逐条核对后再保存。")
        project.needs_confirmation = list(dict.fromkeys([*notes, *project.needs_confirmation]))[:20]
    return result


def _quote(lines: list[str], start: int, end: int, limit: int) -> str | None:
    if not 1 <= start <= end <= len(lines):
        return None
    value = "".join(lines[start - 1:end]).strip()
    return value if value and len(value) <= limit else None


def resolve_references(output: ReferencedResumeOutput, source: str) -> ResumeParseOutput:
    """只接受有效连续范围，正文直接切自该简历版本，避免模型重抄与 OCR 引用偏差。"""
    lines = source.splitlines(keepends=True)
    projects = []
    warnings = list(output.needs_confirmation)
    for candidate in output.projects:
        evidence = _quote(lines, candidate.start_line, candidate.end_line, 20000)
        if evidence is None:
            warnings.append("有候选项目的原文行号无效或范围过长，已排除；请对照原文补充。")
            continue
        facts = []
        notes = list(candidate.needs_confirmation)
        seen: set[tuple[int, int]] = set()
        # 项目的 start/end 只引用标题，不是完整项目正文；以后一项目标题或履历分区作为边界。
        next_start = min((
            item.start_line for item in output.projects
            if candidate.end_line < item.start_line <= item.end_line <= len(lines)
        ), default=len(lines) + 1)
        for line_number in range(candidate.end_line + 1, next_start):
            if _heading(lines[line_number - 1]) in _END_SECTION:
                next_start = line_number
                break
        for fact in candidate.facts:
            if not candidate.start_line <= fact.start_line <= fact.end_line < next_start:
                notes.append("有事实引用超出本项目的原文范围，已排除；请核对项目边界后手动补充。")
                continue
            quote = _quote(lines, fact.start_line, fact.end_line, 10000)
            if quote is None or not fact.fact_type.strip():
                notes.append("有事实原文范围无效或过长，已排除；请对照原文补充。")
                continue
            if (quote.strip("#* ") == candidate.name
                    or (_DATE.fullmatch(quote) and candidate.name in quote)
                    or (quote == evidence and candidate.name in quote)):
                notes.append("已排除重复的项目标题/整体引用；项目描述应逐条对应职责、实现或成果。")
                continue
            key = (fact.start_line, fact.end_line)
            if key in seen:
                continue
            seen.add(key)
            facts.append(ExtractedFact(
                fact_type=fact.fact_type, content=quote, evidence_quote=quote,
                confidence=fact.confidence,
            ))
        projects.append(ExtractedProject(
            **candidate.model_dump(exclude={"start_line", "end_line", "facts", "needs_confirmation"}),
            evidence_quote=evidence, facts=facts, needs_confirmation=notes[:20],
        ))
    return ResumeParseOutput(schema_version="resume_parse.v1", projects=projects,
                             needs_confirmation=warnings[:50])


def _heading(line: str) -> str:
    # 仅用于识别结构；输出引用仍取原文，不保存这里处理过的正文。
    return re.sub(r"\s+", "", line.strip().strip("#*：:【】[] ")).lower()


def local_candidates(source: str) -> ResumeParseOutput:
    """保守识别「项目经历 + 时间 + 标题」、Markdown 标题或显式项目名称。

    不从技能/工作经历推断项目，不推断角色、技术栈或成果。模糊边界保留为待核对项。
    """
    projects: list[ExtractedProject] = []
    warnings = ["本地规则备选，未调用模型。仅按标题、日期、段落分组，请核对项目边界；未推断摘要、角色或技术栈。"]
    active = False
    expect_title = False
    pending_date = ""
    current: ExtractedProject | None = None
    block: list[str] = []

    def flush_fact() -> None:
        nonlocal block
        text = "".join(block).strip()
        block = []
        if not text or current is None:
            return
        # 超长段落只按连续字符范围切开，每一份仍是原文片段。
        for start in range(0, len(text), 10000):
            quote = text[start:start + 10000]
            if not quote.strip():
                continue
            if len(current.facts) >= 40:
                if "事实超过 40 条，仅展示前 40 条；剩余内容仍保留在简历原文。" not in current.needs_confirmation:
                    current.needs_confirmation.append("事实超过 40 条，仅展示前 40 条；剩余内容仍保留在简历原文。")
                break
            current.facts.append(ExtractedFact(fact_type="原文", content=quote,
                evidence_quote=quote, confidence=0))

    def finish_project() -> None:
        nonlocal current
        flush_fact()
        if current is not None:
            if not current.facts:
                current.needs_confirmation.append("未识别到项目正文，请从简历原文补充事实。")
            projects.append(current)
            current = None

    for raw in source.splitlines(keepends=True):
        text = raw.strip()
        heading = _heading(text)
        if heading in _PROJECT_SECTION:
            finish_project()
            active = True; expect_title = True; pending_date = ""
            continue
        if active and heading in _END_SECTION:
            finish_project()
            active = False; expect_title = False; pending_date = ""
            continue
        explicit = _NAMED_PROJECT.match(text.strip("#* "))
        if explicit:
            active = True
        if not active:
            continue
        if not text:
            flush_fact()
            continue
        dated = _DATE.fullmatch(text)
        if dated:
            finish_project()
            pending_date = dated.group("date")
            expect_title = True
            if not dated.group("title"):
                continue
        markdown_title = text.startswith("#") and not _FIELD.match(text.lstrip("# "))
        title = (dated.group("title").strip() if dated and dated.group("title") else
                 explicit.group(1).strip() if explicit else text.lstrip("# ").strip("* "))
        is_title = (explicit is not None or markdown_title or expect_title) and (
            0 < len(title) <= 200 and not _FIELD.match(title)
            and (dated is not None or explicit is not None or not _BULLET.match(text))
        )
        if is_title:
            finish_project()
            if len(projects) >= 15:
                warnings.append("项目超过 15 个，本次只展示前 15 个；其余保留在简历原文。")
                break
            current = ExtractedProject(
                name=title, evidence_quote=text, duration=pending_date or None,
                confidence=0, needs_confirmation=["本地规则分组，不代表已核实；请确认边界及事实归属。"],
            )
            expect_title = False; pending_date = ""
            continue
        if current is not None:
            if _BULLET.match(text) or _FIELD.match(text):
                flush_fact()
            block.append(raw)
    finish_project()
    if not projects:
        warnings.append("未找到可靠项目标题，未把整份简历或任职经历当作项目。可手工添加项目并选取原文，或在新简历版本中标明项目名称与边界。")
    return ResumeParseOutput(schema_version="resume_parse.v1", projects=projects[:15],
                             needs_confirmation=warnings)
