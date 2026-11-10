"""把事件、身份、记忆、提炼串成一条管线。

业务层（网页 `/api/chat`、QQ 适配器）只调用这里的几个函数，不直接碰
`events` / `store` / `identity` 的细节。这样“一轮对话要写什么、按什么顺序
写”只有一份实现，网页和 QQ 不会各写一遍然后慢慢漂移。

所有公开入口都遵守同一条底线：**记忆失败不能影响回复**。写事件、读记忆、
提炼出错时只记日志并返回空值，调用方照常把回复发出去。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace

from app.config import get_settings
from app.embeddings import get_embedder

from app.memory import db, events, identity, merge, retrieval, store
from app.memory.extract import Extractor, Turn
from app.memory.vectors import MEMORIES_VEC
from app.memory.visibility import Viewer

logger = logging.getLogger(__name__)

# 向量不可用时的占位编码器：`available=False` 会让写入跳过向量索引，
# 但记忆本身仍然落库，检索退回重要性/新鲜度排序。
_UNAVAILABLE_EMBEDDER = SimpleNamespace(
    available=False,
    dim=0,
    encode=lambda *args, **kwargs: [],
)

# 持有后台提炼任务的强引用。事件循环只保存弱引用，任务可能在执行前
# 被垃圾回收；这个集合保证它跑完，done 回调负责移除。
_PENDING: set[asyncio.Task] = set()


# ----------------------------------------------------------------------
# 观察者
# ----------------------------------------------------------------------
def web_viewer(conn: sqlite3.Connection, user_id: int) -> Viewer:
    """网页访客的可见范围；只读，不写 `persons`。"""
    key = identity.person_key_web(user_id)
    return Viewer(tuple(identity.linked_keys(conn, key)), "web")


def qq_viewer(
    conn: sqlite3.Connection,
    qq_uin: int | str,
    group_id: int | None,
    binding: object | None,
) -> Viewer:
    """QQ 访客的可见范围；已绑定时顺便补上身份关联。

    `binding` 只要求有 `user_id` 属性（额度库里的绑定对象）。
    """
    bound_user_id = getattr(binding, "user_id", None) if binding else None
    person_key = identity.person_key_qq(qq_uin, bound_user_id)
    channel = "qq_group" if group_id else "qq_private"

    if bound_user_id:
        # 两个 key 指向同一个人：之后网页和 QQ 读到的是同一份个人记忆。
        identity.link_identity(
            conn,
            [identity.person_key_web(bound_user_id), identity.person_key_qq(qq_uin)],
            wp_user_id=bound_user_id,
            qq_uin=str(qq_uin),
        )
        # 还没绑定时的 `qq:<uin>` 数据要搬到账号名下。合并是幂等的，
        # 第一次之后就是空操作，所以放在读路径里也安全。
        merge.merge_person(
            conn,
            source_keys=[identity.person_key_qq(qq_uin)],
            target_key=identity.person_key_web(bound_user_id),
        )
        conn.commit()
        keys = identity.linked_keys(conn, person_key)
    else:
        # 没绑定时只认自己。`persons` 里可能还留着解绑前的旧关联，
        # 拿它扩权等于让已解绑的 QQ 继续读原账号的记忆。
        keys = [person_key]

    return Viewer(tuple(keys), channel, group_id=group_id)


# ----------------------------------------------------------------------
# 事件写入
# ----------------------------------------------------------------------
def _turn_scope(
    channel: str, group_id: int | None, person_key: str
) -> tuple[str, list[str]]:
    """明确“这轮对话发生在哪”：群里就是群可见，其余都是个人可见。"""
    if channel == "qq_group" and group_id:
        return "group", [f"group:{group_id}"]
    return "personal", [person_key]


def _connect():
    db.init_schema()
    return db.connect()


def record_web_user_event(user_id: int, text: str) -> int | None:
    """网页流开始前记下用户说的话；失败返回 `None`。"""
    try:
        conn = _connect()
        try:
            person_key = identity.person_key_web(user_id)
            identity.touch_person(conn, person_key, wp_user_id=user_id)
            scope, audience = _turn_scope("web", None, person_key)
            return events.record_event(
                conn,
                person_key=person_key,
                platform="web",
                channel_id="web",
                scope=scope,
                audience=audience,
                source="user_message",
                content=text,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 记忆库不可用不能影响回复
        logger.warning("记录网页用户事件失败 user=%s", user_id, exc_info=True)
        return None


def _event_content(conn: sqlite3.Connection, event_id: int | None) -> str:
    if event_id is None:
        return ""
    row = conn.execute("SELECT content FROM events WHERE id = ?", (event_id,)).fetchone()
    return str(row["content"]) if row is not None else ""


def record_web_assistant_event(
    user_id: int, text: str, user_event_id: int | None
) -> Turn | None:
    """网页回复正常结束后记下助手说的话，返回可提炼的一轮。"""
    try:
        conn = _connect()
        try:
            person_key = identity.person_key_web(user_id)
            scope, audience = _turn_scope("web", None, person_key)
            assistant_event_id = events.record_event(
                conn,
                person_key=person_key,
                platform="web",
                channel_id="web",
                scope=scope,
                audience=audience,
                source="assistant_message",
                content=text,
            )
            identity.touch_person(conn, person_key, wp_user_id=user_id)
            # 用户原话从时间线里取：签名没有 user_text，但提炼需要完整问答。
            user_text = _event_content(conn, user_event_id)
            return Turn(
                person_key=person_key,
                channel="web",
                user_text=user_text,
                assistant_text=text,
                user_event_id=user_event_id,
                assistant_event_id=assistant_event_id,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("记录网页助手事件失败 user=%s", user_id, exc_info=True)
        return None


def record_web_turn(user_id: int, user_text: str, assistant_text: str) -> Turn | None:
    """一次写入网页的一问一答；测试与命令行使用。"""
    user_event_id = record_web_user_event(user_id, user_text)
    return record_web_assistant_event(user_id, assistant_text, user_event_id)


def record_qq_turn(
    qq_uin: int,
    group_id: int | None,
    binding: object | None,
    user_text: str,
    assistant_text: str,
) -> Turn | None:
    """QQ 回复成功后一次性写入一问一答；失败返回 `None`。"""
    try:
        conn = _connect()
        try:
            viewer = qq_viewer(conn, qq_uin, group_id, binding)
            person_key = viewer.person_keys[0]
            scope, audience = _turn_scope(viewer.channel, group_id, person_key)
            channel_id = f"group:{group_id}" if group_id else f"private:{qq_uin}"

            user_event_id = events.record_event(
                conn,
                person_key=person_key,
                platform="qq",
                channel_id=channel_id,
                scope=scope,
                audience=audience,
                source="user_message",
                content=user_text,
            )
            assistant_event_id = events.record_event(
                conn,
                person_key=person_key,
                platform="qq",
                channel_id=channel_id,
                scope=scope,
                audience=audience,
                source="assistant_message",
                content=assistant_text,
            )
            bound_user_id = getattr(binding, "user_id", None) if binding else None
            identity.touch_person(
                conn,
                person_key,
                wp_user_id=bound_user_id,
                qq_uin=str(qq_uin),
            )
            return Turn(
                person_key=person_key,
                channel=viewer.channel,
                user_text=user_text,
                assistant_text=assistant_text,
                group_id=group_id,
                user_event_id=user_event_id,
                assistant_event_id=assistant_event_id,
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("记录 QQ 对话事件失败 qq=%s", qq_uin, exc_info=True)
        return None


def record_qq_ignored(
    qq_uin: int,
    group_id: int | None,
    binding: object | None,
    user_text: str,
) -> int | None:
    """梅林选择不回复时，只把用户这条写进时间线。

    沉默也是真实发生过的事：不写进去，之后的记忆和主动消息就不知道
    “她当时没接话”。这里不写 assistant 事件，也不触发记忆提炼。
    """
    try:
        conn = _connect()
        try:
            viewer = qq_viewer(conn, qq_uin, group_id, binding)
            person_key = viewer.person_keys[0]
            scope, audience = _turn_scope(viewer.channel, group_id, person_key)
            channel_id = f"group:{group_id}" if group_id else f"private:{qq_uin}"
            event_id = events.record_event(
                conn,
                person_key=person_key,
                platform="qq",
                channel_id=channel_id,
                scope=scope,
                audience=audience,
                source="user_message",
                content=user_text,
            )
            bound_user_id = getattr(binding, "user_id", None) if binding else None
            identity.touch_person(
                conn,
                person_key,
                wp_user_id=bound_user_id,
                qq_uin=str(qq_uin),
            )
            return event_id
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 记不住也不能影响回复链路
        logger.warning("记录 QQ 未回复事件失败 qq=%s", qq_uin, exc_info=True)
        return None


# ----------------------------------------------------------------------
# 记忆读取
# ----------------------------------------------------------------------
def _memory_enabled() -> bool:
    return bool(getattr(get_settings(), "memory_enabled", True))


def web_memory_context(user_id: int, query: str) -> str:
    """网页这一轮要注入的记忆片段；任何异常都返回空字符串。"""
    if not _memory_enabled():
        return ""
    try:
        conn = _connect()
        try:
            return retrieval.build_memory_context(
                web_viewer(conn, user_id), query, conn=conn
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("网页记忆检索失败 user=%s", user_id, exc_info=True)
        return ""


def qq_memory_context(
    qq_uin: int,
    group_id: int | None,
    binding: object | None,
    query: str,
) -> str:
    """QQ 这一轮要注入的记忆片段；任何异常都返回空字符串。"""
    if not _memory_enabled():
        return ""
    try:
        conn = _connect()
        try:
            return retrieval.build_memory_context(
                qq_viewer(conn, qq_uin, group_id, binding), query, conn=conn
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("QQ 记忆检索失败 qq=%s", qq_uin, exc_info=True)
        return ""


# ----------------------------------------------------------------------
# 提炼
# ----------------------------------------------------------------------
async def extract_turn(turn: Turn, extractor: object | None = None) -> int:
    """把一轮对话提炼成记忆并写库，返回成功写入的条数。"""
    try:
        drafts = await (extractor or Extractor()).extract(turn)
    except Exception:  # noqa: BLE001
        logger.warning("记忆提炼失败 person=%s", turn.person_key, exc_info=True)
        return 0
    if not drafts:
        return 0

    if getattr(get_settings(), "memory_vector_enabled", True):
        embedder = get_embedder()
    else:
        embedder = _UNAVAILABLE_EMBEDDER

    written = 0
    try:
        conn = _connect()
        try:
            for draft in drafts:
                try:
                    store.add_memory(conn, draft, embedder=embedder)
                    written += 1
                except Exception:  # noqa: BLE001 - 一条坏数据不该拖垮整轮
                    logger.warning(
                        "写入记忆失败 person=%s", turn.person_key, exc_info=True
                    )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.warning("记忆库不可用，丢弃本轮提炼结果", exc_info=True)
        return 0
    return written


def delete_user_memory(user_id: int) -> dict[str, int]:
    """删除一个 WP 账号名下的全部记忆数据，返回删除条数。

    WordPress 账号是唯一身份，QQ 只是可更换的登录方式：注销账号时，
    这个人的记忆、事件和身份关联都要一起删掉。绑定期写入的事件本来就
    记在 `wp:<id>` 名下，这里再把仍关联着的 QQ key 一并清空。

    与聊天链路相反，这里**不吞异常**：注销流程必须知道删除是否成功，
    失败时要阻止注销，不能留下“账号没了、记忆还在”。
    """
    conn = _connect()
    try:
        deleted = merge.delete_person_data(
            conn, identity.person_key_web(user_id)
        )
        conn.commit()
        return deleted
    finally:
        conn.close()


def _log_task_result(task: asyncio.Task) -> None:
    """后台任务没有 await 的地方，异常必须在这里取走，否则只会被 GC 警告。"""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("记忆提炼任务异常", exc_info=error)


def schedule_extraction(turn: Turn) -> None:
    """在后台提炼，不阻塞回复。没有事件循环（同步测试/CLI）时什么也不做。"""
    if not getattr(get_settings(), "memory_extract_enabled", True):
        return
    try:
        task = asyncio.create_task(extract_turn(turn))
    except RuntimeError:
        # 同步上下文里没有事件循环；提炼本来就只是增强，直接跳过。
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    task.add_done_callback(_log_task_result)
