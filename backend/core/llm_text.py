"""LLM 输出文本工具（收敛各 Agent 重复实现的公共逻辑）

- msg_text(resp)：从 LangChain 消息对象安全取文本（兼容 .text 属性 / str content / 列表 content）
- parse_json_loose(raw)：容错解析 LLM 返回的 JSON——剥 markdown fence、前后缀噪声、
  平衡花括号扫描（支持嵌套对象，如谈判报告的 dimensions 列表套对象）。
  此前 bid_review/procurement 用非嵌套正则、negotiation 自带平衡扫描——统一为最强实现。
"""
import json
import re


def msg_text(resp) -> str:
    if hasattr(resp, "text") and not callable(getattr(resp, "text", None)):
        return resp.text
    if isinstance(resp.content, str):
        return resp.content
    return str(resp.content)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def parse_json_loose(raw: str) -> dict | None:
    """尽力从 LLM 输出中提取一个 JSON 对象；彻底失败返回 None（调用方自行兜底）"""
    if not raw:
        return None
    text = _FENCE_RE.sub(r"\1", raw.strip())
    try:
        obj = json.loads(text.strip())
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except Exception:
                    return None
    return None
