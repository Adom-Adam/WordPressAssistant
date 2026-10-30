"""人设文件的数据模型。

这里用 Pydantic 定义“一个人设版本长什么样”。它的作用和 `schemas.py`
里的请求模型一样：在文件被读进程序时立刻校验，而不是等到拼提示词时
才发现少了一个字段。

人设文件示例（真实内容不要提交到 Git）：

    {
      "schema": "l2d.persona/v1",
      "id": "merlin",
      "version": 1,
      "name": "梅林",
      "core": "……她是谁、怎么说话……",
      "background": "……她为什么在这里……",
      "rules": "……检索、跳转和安全边界……",
      "channels": {
        "web": {"overlay": ""},
        "qq_group": {"overlay": "……群聊额外注意事项……"}
      },
      "metadata": {"source": "current-web-prompt"}
    }

为什么把 `core / background / rules` 分成三段：
- 顺序就是“权重”：越靠前、越长的段落越容易成为模型的默认语气；
- 图书馆身份放在 core 之后，写成“差事/背景”，避免盖过梅林本人；
- 规则只负责事实准确和权限，不参与塑造性格。

`channels` 是频道覆盖层。同一份核心人设，网页和 QQ 群可以用不同的
篇幅、格式和礼仪要求；P1 只用到 `web`，后续接 QQ 时再加 `qq_group`。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 人设文件格式版本。以后字段不兼容时升级这个值，加载器就能明确拒绝旧文件，
# 而不是“少读了几个字段但看起来还能跑”。
PERSONA_SCHEMA = "l2d.persona/v1"


class ChannelOverlay(BaseModel):
    """某个频道的附加规则。

    `overlay` 会被追加到核心人设之后、身份/页面上下文之前。
    `max_sentences` 和 `allow_actions` 目前只是给后续频道适配器读取的元数据，
    P1 不会改变网页行为。
    """

    model_config = ConfigDict(extra="forbid")

    overlay: str = Field(default="", max_length=8_000)
    max_sentences: int | None = Field(default=None, ge=1, le=20)
    allow_actions: bool = True

    @field_validator("overlay")
    @classmethod
    def _strip_overlay(cls, value: str) -> str:
        return value.strip()


class PersonaVersion(BaseModel):
    """一个不可变的人设版本。

    版本文件一旦发布就不应该原地修改——否则“同一版本号”在不同时间
    会产生不同提示词，日志和评测都无法复现。要改内容就新增版本号。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # JSON 里写 "schema"，Python 里叫 schema_version，避免和 Pydantic 的
    # 旧版 `.schema()` 方法重名。
    schema_version: Literal["l2d.persona/v1"] = Field(
        default=PERSONA_SCHEMA,
        alias="schema",
    )

    # 只允许小写字母、数字、点、下划线、连字符，防止用 "../" 跳出人设目录。
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: int = Field(ge=1, le=1_000_000)
    name: str = Field(min_length=1, max_length=120)

    core: str = Field(min_length=1, max_length=20_000)
    background: str = Field(default="", max_length=20_000)
    rules: str = Field(default="", max_length=20_000)

    channels: dict[str, ChannelOverlay] = Field(default_factory=dict)

    # 来源、导入时间、旧版开场对话等说明性信息。
    # 它不参与提示词组装，只用于审计和以后做导入/导出。
    metadata: dict[str, Any] = Field(default_factory=dict)

    # 注意：这里故意不 strip core/background/rules。
    # 旧版 prompts.py 的常量末尾带换行，组装时会形成空行；
    # 为了迁移后提示词逐字节一致，必须保留作者写的空白。
    # 是否为空由 composer 用 `part.strip()` 判断，不会产生空段落。

    @field_validator("channels")
    @classmethod
    def _valid_channel_names(
        cls, value: dict[str, ChannelOverlay]
    ) -> dict[str, ChannelOverlay]:
        for name in value:
            if not name or len(name) > 40 or not name.replace("_", "").isalnum():
                raise ValueError(f"频道名不合法：{name!r}")
        return value
