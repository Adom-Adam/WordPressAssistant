"""表情识别提示词的组装。"""

from __future__ import annotations

from app.stickers import tagging


def test_prompt_keeps_json_braces_and_fills_vocabulary() -> None:
    prompt = tagging._build_prompt(["开心", "冷漠"], ["原神", "Genshin"])

    assert '"description"' in prompt
    assert "开心、冷漠" in prompt
    assert "原神、Genshin" in prompt
