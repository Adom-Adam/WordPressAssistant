"""QQ 消息里的图片、表情包与小黄脸。

解析层先保证“媒体消息不会被当成空消息丢掉”：图片和表情包变成占位符，
QQ 自带表情直接变成文字。视觉理解在下一层按开关补上描述。
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any

import pytest

from dataclasses import replace

from app.config import get_settings

from tests.test_qq_private_chat import (
    bind,
    make_adapter,
    private_event,
)


@pytest.fixture(autouse=True)
def memory_isolation(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite3"))
    monkeypatch.setenv("MEMORY_EXTRACT_ENABLED", "false")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def async_test(fn: Any) -> Any:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def private_media_event(*segments: dict[str, Any], message_id: int = 1) -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": 999,
        "user_id": 10001,
        "message_id": message_id,
        "message": list(segments),
    }


def text_segment(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def image_segment(url: str = "https://gchat.qpic.cn/a.image") -> dict[str, Any]:
    return {"type": "image", "data": {"url": url, "file": "a.image"}}


def mface_segment(url: str = "https://gchat.qpic.cn/b.image") -> dict[str, Any]:
    return {
        "type": "mface",
        "data": {"summary": "[动画表情]", "url": url, "file": "b.image"},
    }


def face_segment(face_id: str = "14") -> dict[str, Any]:
    return {"type": "face", "data": {"id": face_id}}


def test_image_and_sticker_become_placeholders(tmp_path: Any) -> None:
    adapter, _, _ = make_adapter(tmp_path)

    incoming = adapter._parse_message(
        private_media_event(
            text_segment("看这个"),
            image_segment(),
            mface_segment(),
            face_segment("14"),
        )
    )

    assert incoming is not None
    assert incoming.text == "看这个[图片][表情包][微笑]"
    assert [item.kind for item in incoming.attachments] == ["image", "sticker"]
    assert incoming.attachments[0].url == "https://gchat.qpic.cn/a.image"


def test_unknown_face_falls_back_to_generic_label(tmp_path: Any) -> None:
    adapter, _, _ = make_adapter(tmp_path)

    incoming = adapter._parse_message(private_media_event(face_segment("9999")))

    assert incoming is not None
    assert incoming.text == "[表情]"


def test_image_only_private_message_is_no_longer_empty(tmp_path: Any) -> None:
    adapter, _, _ = make_adapter(tmp_path)

    incoming = adapter._parse_message(private_media_event(image_segment()))

    assert incoming is not None
    assert incoming.text == "[图片]"
    assert len(incoming.attachments) == 1


def test_plain_text_event_keeps_no_attachments(tmp_path: Any) -> None:
    adapter, _, _ = make_adapter(tmp_path)

    incoming = adapter._parse_message(private_event(10001, "你好"))

    assert incoming is not None
    assert incoming.text == "你好"
    assert incoming.attachments == ()


def capture_stream(seen: list[list[tuple[str, str]]]) -> Any:
    async def _stream(messages: Any, **kwargs: Any) -> Any:
        seen.append([(message.role, message.content) for message in messages])
        yield {"type": "delta", "content": "看到了"}

    return _stream


def fake_describer(text: str | None = "一只柴犬歪头，配字「不懂就问」") -> Any:
    calls: list[dict[str, Any]] = []

    def _describe(url: str, **kwargs: Any) -> str | None:
        calls.append({"url": url, **kwargs})
        return text

    _describe.calls = calls  # type: ignore[attr-defined]
    return _describe


@async_test
async def test_image_only_private_message_is_described_for_model(tmp_path: Any) -> None:
    seen: list[list[tuple[str, str]]] = []
    describer = fake_describer()
    adapter, sender, store = make_adapter(
        tmp_path, stream=capture_stream(seen), image_describer=describer
    )
    bind(store)

    await adapter.handle_event(private_media_event(image_segment()))

    assert seen == [[("user", "[图片：一只柴犬歪头，配字「不懂就问」]")]]
    assert describer.calls[0]["url"] == "https://gchat.qpic.cn/a.image"
    assert describer.calls[0]["person_key"] == "wp:7"


@async_test
async def test_failed_description_is_marked_for_model(tmp_path: Any) -> None:
    """理解失败时告诉模型“这张图没加载出来”，而不是假装没发图。"""
    seen: list[list[tuple[str, str]]] = []
    adapter, _, store = make_adapter(
        tmp_path, stream=capture_stream(seen), image_describer=fake_describer(None)
    )
    bind(store)

    await adapter.handle_event(
        private_media_event(text_segment("看这个"), image_segment())
    )

    assert seen == [[("user", "看这个[图片：加载失败]")]]


@async_test
async def test_failed_sticker_description_is_marked(tmp_path: Any) -> None:
    seen: list[list[tuple[str, str]]] = []
    adapter, _, store = make_adapter(
        tmp_path, stream=capture_stream(seen), image_describer=fake_describer(None)
    )
    bind(store)

    await adapter.handle_event(private_media_event(mface_segment()))

    assert seen == [[("user", "[表情包：加载失败]")]]


@async_test
async def test_vision_switch_off_keeps_placeholders(tmp_path: Any) -> None:
    seen: list[list[tuple[str, str]]] = []
    describer = fake_describer()
    adapter, _, store = make_adapter(
        tmp_path, stream=capture_stream(seen), image_describer=describer
    )
    bind(store)
    real_runtime = adapter._runtime_settings
    adapter._runtime_settings = lambda: replace(  # type: ignore[method-assign]
        real_runtime(), qq_vision_enabled=False
    )

    await adapter.handle_event(private_media_event(image_segment()))

    assert seen == [[("user", "[图片]")]]
    assert describer.calls == []


@async_test
async def test_addressed_group_image_is_described(tmp_path: Any) -> None:
    seen: list[list[tuple[str, str]]] = []
    describer = fake_describer("一个捂脸的表情包")
    adapter, sender, store = make_adapter(
        tmp_path, stream=capture_stream(seen), image_describer=describer
    )
    bind(store)

    event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999,
        "group_id": 888,
        "user_id": 10001,
        "message_id": 3,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            image_segment(),
        ],
    }
    await adapter.handle_event(event)

    assert seen and seen[-1][-1] == ("user", "[图片：一个捂脸的表情包]")


@async_test
async def test_unaddressed_group_image_is_not_described(tmp_path: Any) -> None:
    seen: list[list[tuple[str, str]]] = []
    describer = fake_describer()
    adapter, _, store = make_adapter(
        tmp_path, stream=capture_stream(seen), image_describer=describer
    )
    bind(store)

    event = {
        "post_type": "message",
        "message_type": "group",
        "self_id": 999,
        "group_id": 888,
        "user_id": 10001,
        "message_id": 4,
        "message": [image_segment()],
    }
    await adapter.handle_event(event)

    assert seen == []
    assert describer.calls == []
