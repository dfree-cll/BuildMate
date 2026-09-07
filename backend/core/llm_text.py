"""LLM 输出文本工具（收敛各 Agent 重复实现的公共逻辑）

- msg_text(resp)：从 LangChain 消息对象安全取文本（兼容 .text 属性 / str content / 列表 content）
- parse_json_loose(raw)：容错解析 LLM 返回的 JSON——剥 markdown fence、前后缀噪声、
  平衡花括号扫描（支持嵌套对象，如谈判报告的 dimensions 列表套对象）。
  此前 bid_review/procurement 用非嵌套正则、negotiation 自带平衡扫描——统一为最强实现。
"""
import json
import re
from collections.abc import Mapping, Sequence


def msg_text(resp) -> str:
    """Extract text from a LangChain-style response without leaking ``None``.

    Providers may return a plain string, a message with ``content`` as a list
    of blocks, or a response exposing a non-callable ``text`` property.  Keep
    this helper total so a provider returning ``None`` cannot crash a fallback
    path while formatting an error response.
    """
    if resp is None:
        return ""
    value = getattr(resp, "text", None)
    if value is not None and not callable(value):
        return value if isinstance(value, str) else str(value)
    if isinstance(resp, str):
        return resp
    content = getattr(resp, "content", resp)
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text") or block.get("content")
                if text is not None:
                    parts.append(str(text))
            elif block is not None:
                parts.append(str(block))
        return "".join(parts)
    return str(content) if content is not None else ""


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def parse_json_loose(raw: str) -> dict | None:
    """尽力从 LLM 输出中提取一个 JSON 对象；彻底失败返回 None（调用方自行兜底）"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _FENCE_RE.sub(r"\1", raw.strip())
    try:
        obj = json.loads(text.strip())
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Locate a balanced object while respecting braces inside JSON strings.
    # This handles common model output such as ``{"note": "use {x}"}``.
    for start, char in enumerate(text):
        if char != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            current = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    in_string = False
                continue
            if current == '"':
                in_string = True
            elif current == "{":
                depth += 1
            elif current == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        break
                    return obj if isinstance(obj, dict) else None
    return None
