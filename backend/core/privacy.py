"""Keep accidentally supplied provider credentials out of project records."""

from __future__ import annotations

import os
import re
from typing import Any


_TOKEN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9_.-]{8,}|"
    r"(?:api[_ -]?key|access[_ -]?token)\s*[:=]\s*\S+|"
    r"(?:密钥|秘钥|令牌|凭证)\s*(?:[:：=]|是|为)\s*[A-Za-z0-9_.-]{8,})",
    re.I,
)
_ENV_NAMES = ("DEEPSEEK_API_KEY", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID", "TAVILY_API_KEY", "NTFY_TOKEN")


def _configured_values() -> list[str]:
    return [value for name in _ENV_NAMES if (value := os.getenv(name, "").strip()) and len(value) >= 6]


def has_credential(text: str) -> bool:
    return bool(_TOKEN.search(text)) or any(value in text for value in _configured_values())


def redact(text: str) -> str:
    for value in _configured_values():
        text = text.replace(value, "[凭证已移除]")
    return _TOKEN.sub("[凭证已移除]", text)


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    return value
