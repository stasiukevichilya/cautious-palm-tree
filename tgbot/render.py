"""Model Markdown -> Telegram HTML messages.

Telegram HTML supports only b/i/s/u/code/pre/a/blockquote, so headings become bold, lists get
text markers and tables are drawn in <pre>. Everything else is escaped text. The answer is split
between top-level blocks so no tag is ever cut; an oversized code block is split by lines and any
other oversized block falls back to plain text.
"""
import html
import re

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

# Telegram counts the limit after entity parsing, in UTF-16 units; keep a margin for emoji.
LIMIT = 3900
PARSER = MarkdownIt("commonmark").enable(["table", "strikethrough"])
INLINE_TAGS = {"strong": "b", "em": "i", "s": "s"}
SAFE_SCHEMES = ("http://", "https://", "tg://")
TAG = re.compile(r"<[^>]+>")


def escape(text):
    return html.escape(text, quote=False)


def plain(markup):
    """Visible text of rendered HTML, for length checks and the plain-text fallback."""
    return html.unescape(TAG.sub("", markup))


def inline(node):
    parts = []
    for child in node.children:
        kind = child.type
        if kind == "text":
            parts.append(escape(child.content))
        elif kind in INLINE_TAGS:
            tag = INLINE_TAGS[kind]
            parts.append(f"<{tag}>{inline(child)}</{tag}>")
        elif kind == "code_inline":
            parts.append(f"<code>{escape(child.content)}</code>")
        elif kind == "link":
            href = str(child.attrs.get("href", ""))
            text = inline(child)
            if href.startswith(SAFE_SCHEMES):
                parts.append(f'<a href="{html.escape(href, quote=True)}">{text}</a>')
            else:
                parts.append(text)
        elif kind == "image":
            parts.append(escape(child.content or str(child.attrs.get("src", ""))))
        elif kind in ("softbreak", "hardbreak"):
            parts.append("\n")
        else:  # html_inline and anything unknown are shown literally
            parts.append(escape(child.content))
    return "".join(parts)


def text_of(node):
    return plain(inline(node)) if node.type == "inline" else "".join(text_of(c) for c in node.children)


def pre(code, language=""):
    language = re.sub(r"[^\w+#.-]", "", language.split()[0]) if language.strip() else ""
    attribute = f' class="language-{language}"' if language else ""
    return f"<pre><code{attribute}>{escape(code.rstrip(chr(10)))}</code></pre>"


def table(node):
    rows = [[text_of(cell).strip() for cell in row.children]
            for section in node.children for row in section.children]
    widths = [max(len(row[i]) for row in rows if i < len(row)) for i in range(max(map(len, rows)))]
    lines = [" | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    if len(lines) > 1:
        lines.insert(1, "-+-".join("-" * width for width in widths))
    return pre("\n".join(lines))


def block(node, quoted=False):
    kind = node.type
    if kind == "paragraph":
        return inline(node.children[0])
    if kind == "heading":
        return f"<b>{inline(node.children[0])}</b>"
    if kind in ("fence", "code_block"):
        return pre(node.content, node.info if kind == "fence" else "")
    if kind in ("bullet_list", "ordered_list"):
        return listing(node, quoted)
    if kind == "blockquote":
        inner = "\n\n".join(block(child, True) for child in node.children)
        return inner if quoted else f"<blockquote>{inner}</blockquote>"  # Telegram cannot nest quotes
    if kind == "table":
        return table(node)
    if kind == "hr":
        return "——————"
    return escape(node.content)  # html_block


def listing(node, quoted):
    items = []
    for number, item in enumerate(node.children, int(node.attrs.get("start", 1))):
        marker = f"{number}." if node.type == "ordered_list" else "•"
        body = "\n".join(block(child, quoted) for child in item.children)
        lines = body.split("\n")
        items.append("\n".join([f"{marker} {lines[0]}"] + ["   " + line for line in lines[1:]]))
    return "\n".join(items)


def split_code(node):
    """An oversized code block becomes several <pre> blocks, cut between lines."""
    language = node.info if node.type == "fence" else ""
    blocks, current = [], ""
    for line in node.content.rstrip("\n").split("\n"):
        while len(line) > LIMIT - 100:
            blocks.append(pre(line[:LIMIT - 100], language))
            line = line[LIMIT - 100:]
        if current and len(current) + len(line) + 1 > LIMIT - 100:
            blocks.append(pre(current, language))
            current = ""
        current = f"{current}\n{line}" if current else line
    return blocks + ([pre(current, language)] if current else [])


def split_plain(text):
    parts = []
    while len(text) > LIMIT:
        cut = text.rfind("\n", 0, LIMIT)
        cut = cut if cut > LIMIT // 2 else LIMIT
        parts.append(escape(text[:cut]))
        text = text[cut:].lstrip("\n")
    return parts + ([escape(text)] if text else [])


def render(markdown):
    """Telegram HTML messages, each at most LIMIT visible characters."""
    blocks = []
    for node in SyntaxTreeNode(PARSER.parse(markdown)).children:
        rendered = block(node)
        if len(plain(rendered)) <= LIMIT:
            blocks.append(rendered)
        elif node.type in ("fence", "code_block"):
            blocks.extend(split_code(node))
        else:
            blocks.extend(split_plain(plain(rendered)))
    messages, current = [], ""
    for rendered in blocks:
        joined = f"{current}\n\n{rendered}" if current else rendered
        if current and len(plain(joined)) > LIMIT:
            messages.append(current)
            joined = rendered
        current = joined
    return messages + [current] if current else messages or [escape(markdown.strip()) or "(пустой ответ)"]
