"""敏感内容识别：把命中规则的记忆标成 `secret`。

方向是刻意不对称的：**宁可误判成 secret**。误判的代价是这条内容不被
自动记起（用户再提一次就能重新记）；漏判的代价是密码、证件号被写进
长期记忆，并在某次对话里被说出来。

只做标记，不改原文。`secret` 的读取规则在 `visibility.py`：仅本人、
仅私密通道。
"""

from __future__ import annotations

import re

_SECRET_PATTERNS = (
    # “密码是 xxx”“Password: xxx”
    re.compile(r"(密码|口令|passw(?:or)?d|passwd)\s*(?:是|为|[:：=])?\s*\S+", re.I),
    # “API key 是 …”“token: …”“密钥：…”“令牌=…”
    re.compile(
        r"(api[_ -]?key|access[_ -]?key|secret|token|密钥|令牌)"
        r"\s*(?:是|为|[:：=])\s*\S+",
        re.I,
    ),
    # 18 位身份证号（末位可能是 X）
    re.compile(r"\b\d{17}[\dXx]\b"),
    # 16–19 位连号：银行卡、长账号
    re.compile(r"\b\d{16,19}\b"),
    # 带标签的账号/住址类信息
    re.compile(r"(银行卡|卡号|账号|住址|家庭住址|门牌号|详细地址)\s*[:：]?\s*\S+"),
)


def detect_sensitivity(text: str) -> str:
    """命中任一规则返回 `"secret"`，否则 `"normal"`。"""
    if not text:
        return "normal"
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return "secret"
    return "normal"
