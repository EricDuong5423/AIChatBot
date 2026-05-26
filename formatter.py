"""
formatter.py — Convert markdown từ LLM sang format khác cho client khác nhau.

Hàm public:
    markdown_to_tmp(text)    — TextMeshPro rich text cho Unity
    markdown_to_plain(text)  — Plain text, strip mọi markdown

Cách dùng: gọi qua endpoint POST /chat?format=tmp|plain|md
- md (default): trả nguyên markdown → frontend web render bằng marked.js
- tmp: trả TMP rich text → Unity TextMeshPro render trực tiếp
- plain: strip sạch markdown → cho client text-only
"""

import re

# Màu hiển thị link / quote — tùy chỉnh nếu cần khớp theme Unity
_LINK_COLOR = "#2563eb"
_QUOTE_COLOR = "#666666"
_CODE_BG = "#0000001a"  # 10% black (alpha hex), TMP đọc 8-digit hex


def markdown_to_tmp(text: str) -> str:
    """
    Convert markdown → TextMeshPro rich text.
    TMP tags hỗ trợ: <b>, <i>, <u>, <s>, <color>, <size>, <link>, <sup>, <sub>.
    KHÔNG hỗ trợ list/table tags → convert thủ công sang ký hiệu (•, indent).

    Best-effort regex. Không phải full parser nên edge case phức tạp có thể sai.
    """
    if not text:
        return ""

    # --- Code blocks ``` ``` (xử lý trước vì có thể chứa các ký tự markdown khác) ---
    def _code_block(m):
        body = m.group(1).strip()
        # TMP không có font monospace built-in → dùng italic + indent để phân biệt
        indented = "\n".join("    " + line for line in body.splitlines())
        return f"<i>\n{indented}\n</i>"
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", _code_block, text, flags=re.DOTALL)

    # --- Inline code `xxx` → italics ---
    text = re.sub(r"`([^`\n]+)`", r"<i>\1</i>", text)

    # --- Headings (#, ##, ###) — to size + bold ---
    text = re.sub(r"^### (.+)$", r"<size=115%><b>\1</b></size>", text, flags=re.MULTILINE)
    text = re.sub(r"^## (.+)$", r"<size=125%><b>\1</b></size>", text, flags=re.MULTILINE)
    text = re.sub(r"^# (.+)$", r"<size=140%><b>\1</b></size>", text, flags=re.MULTILINE)

    # --- Bold-italic ***xxx*** (phải xử lý trước bold/italic) ---
    text = re.sub(r"\*\*\*([^*\n]+)\*\*\*", r"<b><i>\1</i></b>", text)

    # --- Bold **xxx** ---
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)

    # --- Italic *xxx* hoặc _xxx_ ---
    # Negative lookaround tránh match bên trong ** đã convert hoặc __
    text = re.sub(r"(?<![\*\\])\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\\])_([^_\n]+)_(?!_)", r"<i>\1</i>", text)

    # --- Strikethrough ~~xxx~~ ---
    text = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", text)

    # --- Links [text](url) — TMP <link> tag (clickable trong Unity) ---
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
        rf'<link="\2"><color={_LINK_COLOR}><u>\1</u></color></link>',
        text,
    )

    # --- Bare URL (không nằm trong markdown link) → cũng convert ---
    # Match URL không nằm sau ]( hoặc " hoặc =
    text = re.sub(
        r'(?<![\("\=])(https?://[^\s<>\)]+)',
        rf'<link="\1"><color={_LINK_COLOR}><u>\1</u></color></link>',
        text,
    )

    # --- Bullets: "- xxx" hoặc "* xxx" ở đầu dòng → "• xxx" ---
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)

    # --- Numbered list (1. 2. ...) — TMP render OK, giữ nguyên ---

    # --- Blockquotes "> xxx" → màu xám ---
    text = re.sub(rf"^>\s+(.+)$", rf'<color={_QUOTE_COLOR}>│ \1</color>', text, flags=re.MULTILINE)

    # --- Horizontal rule (---, ***, ___) → đường gạch ---
    text = re.sub(r"^[\-\*_]{3,}$", "─" * 30, text, flags=re.MULTILINE)

    # --- Tables: TMP không có table support → giữ nguyên ký tự | (LLM ít khi xuất, OK) ---

    return text


def markdown_to_plain(text: str) -> str:
    """
    Strip toàn bộ markdown, giữ lại nội dung text. Cho client chỉ render plain text.
    """
    if not text:
        return ""

    # Code blocks → giữ body
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", r"\1", text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    # Headings — strip leading #
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    # Bold ***xxx*** → xxx
    text = re.sub(r"\*\*\*([^*\n]+)\*\*\*", r"\1", text)
    # Bold **xxx**
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", text)
    # Italic *xxx* / _xxx_
    text = re.sub(r"(?<![\*\\])\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<![_\\])_([^_\n]+)_(?!_)", r"\1", text)
    # Strikethrough
    text = re.sub(r"~~([^~\n]+)~~", r"\1", text)
    # Links [text](url) → text
    text = re.sub(r"\[([^\]\n]+)\]\([^)]+\)", r"\1", text)
    # Bullets → "• "
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)
    # Blockquotes — strip ">"
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    # Horizontal rule
    text = re.sub(r"^[\-\*_]{3,}$", "", text, flags=re.MULTILINE)

    return text


def convert(text: str, fmt: str) -> str:
    """Dispatch theo format. Default 'md' = no-op."""
    if fmt == "tmp":
        return markdown_to_tmp(text)
    if fmt == "plain":
        return markdown_to_plain(text)
    return text  # 'md' hoặc unknown → giữ nguyên markdown
