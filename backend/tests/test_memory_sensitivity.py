from __future__ import annotations

import pytest

from app.memory.sensitivity import detect_sensitivity


@pytest.mark.parametrize(
    "text",
    [
        "我的密码是 abc12345",
        "Password: hunter2",
        "API key 是 sk-abcdef123456",
        "身份证号 110101199001011234",
        "银行卡 6222021234567890",
        "家庭住址：某某路 12 号 3 单元 501",
        "我的 token 是 abcd1234",
    ],
)
def test_secret_like_text_is_detected(text: str) -> None:
    assert detect_sensitivity(text) == "secret"


@pytest.mark.parametrize(
    "text",
    [
        "今天天气很好",
        "我在学 ROS2 的节点通信",
        "周末去看了场电影",
        "这个项目拖了三周",
    ],
)
def test_normal_text_stays_normal(text: str) -> None:
    assert detect_sensitivity(text) == "normal"
