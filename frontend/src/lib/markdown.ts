/**
 * 轻量 Markdown 解析：只生成数据节点，由 Vue 模板渲染，不使用 innerHTML。
 * 覆盖题库答案常见写法：标题、段落、列表、代码围栏、引用、分隔线，
 * 行内加粗、斜体、删除线、行内代码、链接（仅 http/https）与反斜杠转义。
 */

export interface InlineNode {
  type: 'text' | 'break' | 'code' | 'strong' | 'emphasis' | 'strike' | 'link'
  text: string
  href: string
  children: InlineNode[]
}

export interface BlockNode {
  type: 'paragraph' | 'heading' | 'list' | 'code' | 'quote' | 'rule'
  level: number
  ordered: boolean
  start: number
  language: string
  text: string
  children: InlineNode[]
  items: InlineNode[][]
}

const ESCAPABLE = new Set(['\\', '`', '*', '_', '{', '}', '[', ']', '(', ')', '#', '+', '-', '.', '!', '|', '>', '~', '<'])
const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)\s*$/
const HEADING = /^ {0,3}(#{1,6})\s+(.*)$/
const RULE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/
const QUOTE = /^ {0,3}>\s?(.*)$/
const LIST_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/
const LINK = /^\[([^\]\n]*)\]\(([^()\s]+)\)/

function textNode(text: string): InlineNode {
  return { type: 'text', text, href: '', children: [] }
}

function emptyNode(type: InlineNode['type'], text = ''): InlineNode {
  return { type, text, href: '', children: [] }
}

function isWordCharacter(value: string | undefined): boolean {
  return value !== undefined && /[\w\u4e00-\u9fff]/.test(value)
}

function scan(source: string, depth: number): InlineNode[] {
  const nodes: InlineNode[] = []
  let buffer = ''
  let index = 0
  const flush = (): void => {
    if (buffer) {
      nodes.push(textNode(buffer))
      buffer = ''
    }
  }

  while (index < source.length) {
    const character = source[index]
    if (character === '\\' && index + 1 < source.length && ESCAPABLE.has(source[index + 1])) {
      buffer += source[index + 1]
      index += 2
      continue
    }
    if (character === '\n') {
      flush()
      nodes.push(emptyNode('break'))
      index += 1
      continue
    }
    if (character === '`') {
      const close = source.indexOf('`', index + 1)
      if (close > index + 1 && !source.slice(index + 1, close).includes('\n')) {
        flush()
        nodes.push(emptyNode('code', source.slice(index + 1, close)))
        index = close + 1
        continue
      }
    }
    if (depth < 4) {
      if (character === '*') {
        const strong = matchDelimited(source, '**', index + 2)
        if (strong) {
          flush()
          nodes.push({ type: 'strong', text: '', href: '', children: scan(strong.content, depth + 1) })
          index = strong.end
          continue
        }
      }
      if (character === '~' && source[index + 1] === '~') {
        const strike = matchDelimited(source, '~~', index + 2)
        if (strike) {
          flush()
          nodes.push({ type: 'strike', text: '', href: '', children: scan(strike.content, depth + 1) })
          index = strike.end
          continue
        }
      }
      if (character === '*' || character === '_') {
        const allowed =
          character === '*'
            ? source[index + 1] !== undefined && !/\s/.test(source[index + 1])
            : !isWordCharacter(source[index - 1]) && source[index + 1] !== undefined && !/\s/.test(source[index + 1])
        const emphasis = allowed ? matchDelimited(source, character, index + 1) : null
        if (emphasis) {
          flush()
          nodes.push({ type: 'emphasis', text: '', href: '', children: scan(emphasis.content, depth + 1) })
          index = emphasis.end
          continue
        }
      }
      if (character === '[') {
        const link = LINK.exec(source.slice(index))
        if (link && /^https?:\/\//i.test(link[2])) {
          flush()
          nodes.push({
            type: 'link',
            text: '',
            href: link[2],
            children: scan(link[1], depth + 1),
          })
          index += link[0].length
          continue
        }
      }
    }
    buffer += character
    index += 1
  }
  flush()
  return nodes
}

function matchDelimited(source: string, marker: string, from: number): { content: string; end: number } | null {
  let cursor = from
  while (cursor < source.length) {
    const close = source.indexOf(marker, cursor)
    if (close < 0) return null
    if (close > from) return { content: source.slice(from, close), end: close + marker.length }
    cursor = close + marker.length
  }
  return null
}

export function parseInline(source: string): InlineNode[] {
  return scan(source, 0)
}

function startsBlock(line: string): boolean {
  return (
    FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || LIST_ITEM.test(line)
  )
}

function paragraph(type: BlockNode['type'], lines: string[]): BlockNode {
  return {
    type,
    level: 0,
    ordered: false,
    start: 1,
    language: '',
    text: '',
    children: parseInline(lines.join('\n')),
    items: [],
  }
}

export function parseBlocks(source: string): BlockNode[] {
  const lines = source.replace(/\r\n?/g, '\n').split('\n')
  const blocks: BlockNode[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]
    if (!line.trim()) {
      index += 1
      continue
    }

    const fence = FENCE.exec(line)
    if (fence) {
      const marker = fence[1]
      const body: string[] = []
      index += 1
      while (index < lines.length) {
        const closing = lines[index].trimEnd()
        if (closing.startsWith(marker[0].repeat(marker.length)) && !closing.slice(marker.length).trim()) {
          index += 1
          break
        }
        body.push(lines[index])
        index += 1
      }
      blocks.push({
        type: 'code',
        level: 0,
        ordered: false,
        start: 1,
        language: fence[2],
        text: body.join('\n'),
        children: [],
        items: [],
      })
      continue
    }

    const heading = HEADING.exec(line)
    if (heading) {
      blocks.push({
        type: 'heading',
        level: heading[1].length,
        ordered: false,
        start: 1,
        language: '',
        text: '',
        children: parseInline(heading[2].trim()),
        items: [],
      })
      index += 1
      continue
    }

    if (RULE.test(line)) {
      blocks.push({ type: 'rule', level: 0, ordered: false, start: 1, language: '', text: '', children: [], items: [] })
      index += 1
      continue
    }

    if (QUOTE.test(line)) {
      const quoted: string[] = []
      while (index < lines.length) {
        const match = QUOTE.exec(lines[index])
        if (!match) break
        quoted.push(match[1])
        index += 1
      }
      blocks.push(paragraph('quote', quoted))
      continue
    }

    const item = LIST_ITEM.exec(line)
    if (item) {
      const ordered = /\d/.test(item[2])
      const start = ordered ? Number.parseInt(item[2], 10) || 1 : 1
      const items: InlineNode[][] = []
      let current: string[] = []
      const flushItem = (): void => {
        if (current.length) {
          items.push(parseInline(current.join('\n')))
          current = []
        }
      }
      while (index < lines.length) {
        const row = lines[index]
        if (!row.trim()) break
        const next = LIST_ITEM.exec(row)
        if (next) {
          flushItem()
          current.push(next[3])
          index += 1
          continue
        }
        if (startsBlock(row)) break
        current.push(row.trim())
        index += 1
      }
      flushItem()
      blocks.push({
        type: 'list',
        level: 0,
        ordered,
        start,
        language: '',
        text: '',
        children: [],
        items,
      })
      continue
    }

    const collected: string[] = []
    while (index < lines.length && lines[index].trim() && !startsBlock(lines[index])) {
      collected.push(lines[index])
      index += 1
    }
    blocks.push(paragraph('paragraph', collected))
  }

  return blocks
}
