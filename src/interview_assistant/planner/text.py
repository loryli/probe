"""消费层纯文本化：逐行保序，只去排版标记，不改内容与顺序。

仅用于把题库题干压缩成适合模型读取的短文本。题库原文、导入来源全文、
内容版本与计划快照一律保持逐字原文；本模块的结果不写回题库。
项目事实也保留原文，以免破坏证据引用的逐字可核验性。
"""

import re

# Markdown 转义：反斜杠后跟标点或空白（\、 \. \- \ 等），去掉反斜杠保留后随字符。
_ESCAPED = re.compile(r"\\(?=[^\w])")
_LEADING_LABEL = re.compile(r"^[ \t]*【[^】]{1,24}】[ \t]*")
_LEADING_NUMBER = re.compile(r"^[ \t]*\d{1,3}[ \t]*[.、)）][ \t]*")
_LEADING_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+")
_LEADING_QUOTE = re.compile(r"^[ \t]*>[ \t]?")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+")
_FENCE = re.compile(r"^[ \t]*`{3,}")


def plain_text(value: str) -> str:
    """保序清理：不重排行、不删除非空行、不合并段落、不改文字先后。"""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    cleaned: list[str] = []
    for raw in normalized.split("\n"):
        line = raw.rstrip()
        if raw.startswith(("    ", "\t")) or _FENCE.match(raw):
            cleaned.append(line)
            continue
        line = _LEADING_LABEL.sub("", line)
        line = _ESCAPED.sub("", line)
        line = _LEADING_NUMBER.sub("", line)
        line = _LEADING_BULLET.sub("", line)
        line = _LEADING_QUOTE.sub("", line)
        line = _HEADING.sub("", line)
        line = line.replace("**", "").replace("__", "").replace("~~", "")
        cleaned.append(line)
    result: list[str] = []
    blank = 0
    for line in cleaned:
        if line.strip():
            blank = 0
            result.append(line)
        else:
            blank += 1
            if blank == 1:
                result.append("")
    return "\n".join(result).strip()
