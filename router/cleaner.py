"""Strip leaked role tags, metadata, and trailing fragments from model output."""

from __future__ import annotations

import json
import re

ROLE_LINE = re.compile(
    r"^\s*\[(?:User|Assistant|System|Human|AI|grounding presence)\]\s*:?\s*",
    re.IGNORECASE,
)
ROLE_INLINE = re.compile(
    r"\[(?:User|Assistant|System|Human|AI|grounding presence)\]\s*:?",
    re.IGNORECASE,
)
GROUNDING = re.compile(r"\[?\s*grounding presence\s*\]?:?", re.IGNORECASE)
FENCE = re.compile(
    r"```(?:json|xml|html|yaml|yml)?\s*\n.*?```",
    re.IGNORECASE | re.DOTALL,
)
XML_BLOCK = re.compile(
    r"<(?:think|thought|reasoning|meta|metadata|tool_call|tool|function|json|output|response)[^>]*>"
    r".*?"
    r"</(?:think|thought|reasoning|meta|metadata|tool_call|tool|function|json|output|response)>",
    re.IGNORECASE | re.DOTALL,
)
XML_TAG = re.compile(
    r"</?(?:think|thought|reasoning|meta|metadata|tool_call|tool|function|json)[^>]*>",
    re.IGNORECASE,
)
SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*")
META_KEYS = {
    "type",
    "role",
    "name",
    "function",
    "tool",
    "arguments",
    "parameters",
    "metadata",
    "tool_calls",
}


def _replace_json_blob(blob: str) -> str:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return blob
    if not isinstance(data, dict):
        return blob
    keys = {str(k).lower() for k in data}
    for field in ("content", "text", "reply"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value
    if keys & META_KEYS:
        return ""
    return blob


def _strip_json_metadata(text: str) -> str:
    pieces: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            pieces.append(text[i])
            i += 1
            continue
        depth = 0
        in_str = False
        escape = False
        j = i
        matched = False
        while j < n:
            ch = text[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        pieces.append(_replace_json_blob(text[i : j + 1]))
                        i = j + 1
                        matched = True
                        break
            j += 1
        if not matched:
            pieces.append(text[i])
            i += 1
    return "".join(pieces)


def _natural_decay(text: str) -> str:
    """Trim trailing incomplete generation at the last sentence boundary."""
    text = text.strip()
    if not text:
        return text
    ends = list(SENTENCE_END.finditer(text))
    if not ends:
        return text
    last = ends[-1]
    if text[last.end() :].strip():
        return text[: last.end()].strip()
    return text


def clean_output(text: str | None) -> str:
    if not text:
        return ""

    cleaned = str(text)
    cleaned = FENCE.sub("", cleaned)
    cleaned = XML_BLOCK.sub("", cleaned)
    cleaned = XML_TAG.sub("", cleaned)
    cleaned = _strip_json_metadata(cleaned)

    lines = []
    for line in cleaned.splitlines():
        line = ROLE_LINE.sub("", line)
        line = ROLE_INLINE.sub("", line)
        line = GROUNDING.sub("", line)
        lines.append(line)
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return _natural_decay(cleaned)
