"""把“默认文案 + 后台覆盖”解析成这一轮真正要发出去的话。

====================================================================
为什么单独一层
====================================================================
文案是运营内容，不是逻辑：站长可能今天想换个说法，明天又改回来。
但适配器里到处是 `commands.XXX`，如果每处都写一遍“有覆盖就用覆盖”，
迟早有一处漏掉，出现前后文风不一致。

所以规则只写一次：**留空就用默认，非空就用覆盖，最后统一替换
`{place}`**。适配器只拿一个 `QQCopy`，不关心值是从哪来的。

====================================================================
为什么用 replace 而不是 format
====================================================================
管理员可能写颜文字 `{>_<}`、写代码片段、写 JSON 例子。`str.format`
遇到不认识的花括号会抛 KeyError，一条消息就变成 500。这里只替换
`{place}` 这一个确定的占位符，别的花括号原样保留。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.channels.qq import commands
from app.runtime_settings import RuntimeSettings


@dataclass(frozen=True, slots=True)
class QQCopy:
    """一次解析结果：所有字段都是可以直接发出去的成品文本。"""

    private_guide: str
    group_guide: str
    anon_first_hint: str
    anon_memory_hint: str
    anon_quota_user: str
    anon_quota_global: str
    anon_group_ask: str

    @classmethod
    def from_runtime(cls, runtime: RuntimeSettings, *, place: str = "") -> QQCopy:
        """按运行时设置解析；`place` 是站点地址，未配置时传空串。"""

        def pick(override: str, default: str) -> str:
            text = (override or "").strip()
            return text or default

        return cls(
            private_guide=commands._render(
                pick(runtime.copy_private_guide, commands.PRIVATE_GUIDE_TEMPLATE),
                place,
            ),
            group_guide=commands._render(
                pick(runtime.copy_group_guide, commands.GROUP_GUIDE), place
            ),
            anon_first_hint=commands._render(
                pick(
                    runtime.copy_anon_first_hint,
                    commands.ANON_FIRST_HINT_TEMPLATE,
                ),
                place,
            ),
            anon_memory_hint=commands._render(
                pick(
                    runtime.copy_anon_memory_hint,
                    commands.ANON_MEMORY_HINT_TEMPLATE,
                ),
                place,
            ),
            anon_quota_user=commands._render(
                pick(runtime.copy_anon_quota_user, commands.ANON_QUOTA_USER), place
            ),
            anon_quota_global=commands._render(
                pick(runtime.copy_anon_quota_global, commands.ANON_QUOTA_GLOBAL),
                place,
            ),
            anon_group_ask=commands._render(
                pick(runtime.copy_anon_group_ask, commands.ANON_GROUP_ASK), place
            ),
        )


def site_place(settings: object) -> str:
    """从 Settings 里取站点地址，给 `QQCopy.from_runtime(place=...)` 用。"""
    return str(getattr(settings, "wp_site_url", "") or "")
