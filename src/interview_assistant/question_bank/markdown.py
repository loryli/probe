"""Deterministic Markdown extraction; source text is data and is never executed."""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

ImportStrategy = Literal["auto", "heading_2", "heading_3", "qa"]
PARSER_VERSION = "markdown-v1"
MAX_FILE_BYTES = 1024 * 1024
MAX_CANDIDATES = 500
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
QUESTION = re.compile(r"^[ \t]*(?:\*\*)?(?:Q|Question|问|问题|面试题)[：:](?:\*\*)?[ \t]*", re.I)
ANSWER = re.compile(
    r"^[ \t]*(?:\*\*)?(?:A|Answer|答|答案)[：:](?:\*\*)?[ \t]*"
    r"|【标准回答(?:话术)?】",
    re.I,
)


class MarkdownLimitError(ValueError):
    pass


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    question_text: str
    standard_answer_text: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    heading_path: list[str]
    warnings: list[str]
    recommended: bool


@dataclass(frozen=True)
class Line:
    start: int
    end: int
    content_start: int
    number: int
    text: str
    visible: bool


@dataclass(frozen=True)
class ParsedMarkdown:
    strategy: ImportStrategy
    candidates: list[Candidate]
    warnings: list[str]


def _lines(content: str) -> tuple[list[Line], bool]:
    lines = []
    position = 0
    fence_character = ""
    fence_length = 0
    for number, raw in enumerate(content.splitlines(keepends=True), 1):
        text = raw.rstrip("\r\n")
        if number == 1:
            text = text.removeprefix("\ufeff")
        marker = FENCE.match(text)
        visible = not fence_character and marker is None and not raw.startswith(("    ", "\t"))
        if fence_character:
            if (
                marker
                and marker[1][0] == fence_character
                and len(marker[1]) >= fence_length
                and not marker[2].strip()
            ):
                fence_character = ""
        elif marker:
            fence_character, fence_length = marker[1][0], len(marker[1])
        content_start = position + int(number == 1 and raw.startswith("\ufeff"))
        lines.append(Line(position, position + len(raw), content_start, number, text, visible))
        position += len(raw)
    return lines, bool(fence_character)


def _marker(lines: list[Line], pattern: re.Pattern[str]) -> tuple[int, int] | None:
    for line in lines:
        match = pattern.search(line.text) if line.visible else None
        if match:
            return line.content_start + match.start(), line.content_start + match.end()
    return None


def parse_markdown(content: str, strategy: ImportStrategy = "auto") -> ParsedMarkdown:
    lines, unclosed_fence = _lines(content)
    headings: list[tuple[int, re.Match[str]]] = []
    for i, line in enumerate(lines):
        match = HEADING.match(line.text) if line.visible else None
        if match is not None:
            headings.append((i, match))
    levels = {len(match[1]) for _, match in headings}
    resolved = strategy
    if strategy == "auto":
        has_qa = any(line.visible and QUESTION.match(line.text) for line in lines)
        resolved = (
            "heading_3" if 3 in levels else "qa" if has_qa else "heading_2" if 2 in levels else "qa"
        )
    ranges: list[tuple[int, int, str, list[str]]] = []
    if resolved in {"heading_2", "heading_3"}:
        level = 2 if resolved == "heading_2" else 3
        if sum(len(match[1]) == level for _, match in headings) > MAX_CANDIDATES:
            raise MarkdownLimitError("单次最多识别 500 道候选题，请将文件拆小后重试。")
        parents: dict[int, str] = {}
        for position, (start, match) in enumerate(headings):
            current_level = len(match[1])
            parents = {key: value for key, value in parents.items() if key < current_level}
            if current_level == level:
                stop = next(
                    (
                        index
                        for index, following in headings[position + 1 :]
                        if len(following[1]) <= level
                    ),
                    len(lines),
                )
                ranges.append((start, stop, match[2], list(parents.values())))
            parents[current_level] = match[2]
    else:
        starts = [i for i, line in enumerate(lines) if line.visible and QUESTION.match(line.text)]
        if len(starts) > MAX_CANDIDATES:
            raise MarkdownLimitError("单次最多识别 500 道候选题，请将文件拆小后重试。")
        for position, start in enumerate(starts):
            stop = starts[position + 1] if position + 1 < len(starts) else len(lines)
            # Category headings delimit the preceding answer, even without a next Q.
            stop = next((i for i, _ in headings if start < i < stop), stop)
            path = [match[2] for i, match in headings if i < start]
            ranges.append((start, stop, "", path[-2:]))
    if len(ranges) > MAX_CANDIDATES:
        raise MarkdownLimitError("单次最多识别 500 道候选题，请将文件拆小后重试。")
    candidates: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for start, stop, title, path in ranges:
        section = lines[start:stop]
        body = section[1:] if resolved != "qa" else section
        question_marker = _marker(body, QUESTION)
        answer_marker = _marker(body, ANSWER)
        warnings = []
        question_text = title
        answer_start = section[0].end
        if question_marker and (answer_marker is None or question_marker[0] < answer_marker[0]):
            end = answer_marker[0] if answer_marker else section[0].end
            if answer_marker is None:
                marker_line = next(
                    line for line in body if line.start <= question_marker[0] < line.end
                )
                end = marker_line.end
            question_text = content[question_marker[1] : end].rstrip("\r\n")
            answer_start = answer_marker[0] if answer_marker else end
        if not question_text.strip():
            warnings.append("题干为空，此段不能入库。")
        answer_text = content[answer_start : section[-1].end]
        if not answer_text.strip():
            warnings.append("未填写答案，不会自动补写。")
        if answer_marker is None and answer_text.strip():
            warnings.append("没有明确的答案标记，请核对正文是否为答案。")
        obvious_question = bool(re.match(r"\d+[\\.、)）]", question_text)) or any(
            mark in question_text for mark in ("？", "?")
        )
        recommended = bool(
            question_text.strip() and answer_text.strip() and (answer_marker or obvious_question)
        )
        if not recommended and question_text.strip() and answer_text.strip():
            warnings.append("可能是说明性段落，默认不选中；请人工核对。")
        raw = content[section[0].start : section[-1].end]
        if re.search(r"!\[|<img\b", raw, re.I):
            warnings.append("包含图片标记：保留原文，不识别或读取图片。")
        fingerprint = (question_text, answer_text)
        if fingerprint in seen:
            warnings.append("与文件中前面的题目和答案完全相同，默认不选中。")
            recommended = False
        seen.add(fingerprint)
        candidates.append(
            Candidate(
                index=len(candidates),
                question_text=question_text,
                standard_answer_text=answer_text,
                start_offset=section[0].start,
                end_offset=section[-1].end,
                start_line=section[0].number,
                end_line=section[-1].number,
                heading_path=path,
                warnings=warnings,
                recommended=recommended,
            )
        )
    warnings = ["预览只保存导入草稿；勾选并确认后才创建题目。"]
    if unclosed_fence:
        warnings.append("文件有未闭合的代码围栏，其后的内容未按标题拆分，请核对原文。")
    if not candidates:
        warnings.append("没有识别到题目，请切换识别方式或调整 Markdown 标题、Q/A 标记。")
    else:
        warnings.append("题目范围外的前言、分类标题等保留在来源文件中，不单独入库。")
    return ParsedMarkdown(resolved, candidates, warnings)
