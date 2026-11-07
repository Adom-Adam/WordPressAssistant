"""读取记忆前的可见性硬过滤。

这是整个记忆系统里最不能出错的一段：一旦这里放宽，A 的私事就会在
群里或 B 的对话里被说出来。所以规则写得非常直白，宁可多挡：

    world     → 只要不是 secret
    personal  → 必须包含本人的某个 key；private/secret 还要求私密通道
    group     → 必须包含当前群；secret 一律不可见
    merlin    → 只有内部反思上下文可见（普通用户永远看不到）

`sql_filter` 只是把范围预先压小的索引条件；调用方拿到行之后必须再用
`is_visible` 逐条复检。两处规则一致，SQL 负责快，Python 负责准。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Viewer:
    """一次读取请求的“观察者”。

    `person_keys` 是同一个人的全部身份（绑定后可能是 `wp:12` + `qq:...`）。
    `channel` 取值：`web` / `qq_private` / `qq_group`。
    """

    person_keys: tuple[str, ...]
    channel: str
    group_id: int | None = None
    include_merlin: bool = False

    @property
    def is_private(self) -> bool:
        return self.channel in {"web", "qq_private"}

    @property
    def group_key(self) -> str | None:
        if self.group_id is None:
            return None
        return f"group:{self.group_id}"


def is_visible(
    *,
    scope: str,
    audience: list[str],
    sensitivity: str,
    viewer: Viewer,
) -> bool:
    """这条记忆/事件能否给这个观察者看。"""
    if scope == "world":
        return sensitivity != "secret"

    if scope == "personal":
        if not any(key in audience for key in viewer.person_keys):
            return False
        # 私密与机密内容只允许在私聊/网页里交还给本人。
        if sensitivity in {"private", "secret"}:
            return viewer.is_private
        return True

    if scope == "group":
        return (
            viewer.group_key is not None
            and viewer.group_key in audience
            and sensitivity != "secret"
        )

    if scope == "merlin":
        return viewer.include_merlin

    # 未知 scope 一律不可见：新增取值时先在这里显式加分支。
    return False


def sql_filter(viewer: Viewer) -> tuple[str, list[Any]]:
    """返回可直接拼进 WHERE 的表达式与参数。

    注意它不处理 `sensitivity`：`audience_key` 已经把范围压到很小，
    敏感级别交给 Python 判定，避免两处规则各写一遍后慢慢漂移。
    """
    clauses: list[str] = []
    params: list[Any] = []

    keys = list(viewer.person_keys)
    if keys:
        placeholders = ",".join("?" for _ in keys)
        clauses.append(f"(scope = 'personal' AND audience_key IN ({placeholders}))")
        params.extend(keys)

    if viewer.group_key:
        clauses.append("(scope = 'group' AND audience_key = ?)")
        params.append(viewer.group_key)

    clauses.append("(scope = 'world')")

    if viewer.include_merlin and keys:
        placeholders = ",".join("?" for _ in keys)
        clauses.append(f"(scope = 'merlin' AND audience_key IN ({placeholders}))")
        params.extend(keys)

    return "(" + " OR ".join(clauses) + ")", params
