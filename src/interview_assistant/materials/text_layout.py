"""确定性的简历排版整理：只处理顺序、空白和断行，不推测或补写事实。"""

import re
from dataclasses import dataclass
from statistics import median
from typing import Any

_HEADINGS = {
    "基本信息", "个人信息", "联系方式", "工作经历", "工作经验", "实习经历", "实习经验",
    "项目经历", "项目经验", "专业技能", "职业技能", "技术栈", "后端技能", "大模型应用开发",
    "获奖情况", "荣誉奖项", "教育背景", "教育经历", "自我评价", "个人优势",
}
_LABELS = ("姓名", "年龄", "工作年限", "学历", "电话", "手机", "邮箱", "毕业院校", "求职意向")
_BULLET = re.compile(r"^(?:[·•●▪◦◆◇*+\-]\s*|\d{1,2}[、.)]\s+)")
_DATE = re.compile(r"^\d{4}[./年-]\d{1,2}")
_LABEL = re.compile(r"^(?:" + "|".join(_LABELS) + r")\s*[:：]")
_END = re.compile(r"[。！？!?；;：:]$")
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]$")
_ASCII_START = re.compile(r"^[A-Za-z0-9_]")


def _heading(line: str) -> bool:
    return line.lstrip("# ").rstrip("：:") in _HEADINGS


def _boundary(line: str) -> bool:
    return bool(_heading(line) or _BULLET.match(line) or _DATE.match(line)
                or _LABEL.match(line) or line.startswith(("【", "#", "```", "~~~"))
                or (len(line) < 20 and re.search(r"[←↑↓↔]", line)))


def _join(previous: str, current: str) -> str:
    separator = " " if _ASCII_WORD.search(previous) and _ASCII_START.match(current) else ""
    return previous + separator + current


def format_resume_text(source: str) -> str:
    """保留词句、数字和未知杂字符；短行不做无依据拼接。"""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    # 仅修复有冒号佐证的已知字段标签，如「姓\n名：」，不拼接任意汉字。
    for label in _LABELS:
        pattern = r"(?<![\u4e00-\u9fff])" + r"[\s\u3000]*".join(label) + r"(?=\s*[:：])"
        normalized = re.sub(pattern, label, normalized)
    output: list[str] = []
    blank = False
    fence: str | None = None
    for raw in normalized.split("\n"):
        line = raw.strip()
        marker = line[:3] if line.startswith(("```", "~~~")) else None
        if fence is not None:
            output.append(raw)
            if marker == fence:
                fence = None
            continue
        if marker:
            if output and output[-1]:
                output.append("")
            output.append(raw)
            fence = marker
            blank = False
            continue
        if not line:
            blank = True
            continue
        previous = output[-1] if output else ""
        # 长行且句子未结束，下一行又不是标题/条目/字段时，视为排版折行。
        continuation = (
            len(previous) >= 30 and not _END.search(previous)
            and not _heading(previous) and not _DATE.match(previous)
            and not _LABEL.match(previous) and not _boundary(line)
            and "   " not in previous and "   " not in line
        )
        if continuation:
            output[-1] = _join(previous, line)
        else:
            if output and previous and (blank or _heading(line) or _heading(previous)):
                output.append("")
            output.append(line)
        blank = False
    return "\n".join(output).strip()


@dataclass
class _Box:
    text: str
    left: float
    top: float
    right: float
    bottom: float

    @property
    def center(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return self.bottom - self.top


def layout_ocr_text(texts: list[str], boxes: Any) -> str:
    """利用四角坐标合并同一视觉行，再整理段落；无坐标时保守退回文本整理。"""
    fallback = "\n".join(texts)
    if boxes is None or len(boxes) != len(texts):
        return format_resume_text(fallback)
    blocks: list[_Box] = []
    try:
        for text, corners in zip(texts, boxes, strict=True):
            xs = [float(point[0]) for point in corners]
            ys = [float(point[1]) for point in corners]
            if not xs or not ys or max(ys) <= min(ys):
                return format_resume_text(fallback)
            blocks.append(_Box(text, min(xs), min(ys), max(xs), max(ys)))
    except (ValueError, TypeError, IndexError):
        return format_resume_text(fallback)
    if not blocks:
        return ""
    typical_height = median(block.height for block in blocks)
    rows: list[list[_Box]] = []
    for block in sorted(blocks, key=lambda value: (value.center, value.left)):
        row = next((candidate for candidate in reversed(rows[-4:])
                    if abs(median(item.center for item in candidate) - block.center)
                    <= min(typical_height, block.height) * 0.5), None)
        if row is None:
            rows.append([block])
        else:
            row.append(block)
    lines: list[str] = []
    previous_bottom: float | None = None
    for row in sorted(rows, key=lambda values: min(item.top for item in values)):
        row.sort(key=lambda value: value.left)
        top, bottom = min(item.top for item in row), max(item.bottom for item in row)
        if previous_bottom is not None and top - previous_bottom > typical_height:
            lines.append("")
        line = row[0].text
        for previous, current in zip(row, row[1:]):
            gap = current.left - previous.right
            if gap > typical_height * 3:
                separator = "   "  # 同行的多个信息栏保持间隔，不强行拼成一句话。
            elif gap <= typical_height * 0.7 and not (
                _ASCII_WORD.search(line) and _ASCII_START.match(current.text)
            ):
                separator = ""
            else:
                separator = " "
            line += separator + current.text
        lines.append(line)
        previous_bottom = bottom
    return format_resume_text("\n".join(lines))
