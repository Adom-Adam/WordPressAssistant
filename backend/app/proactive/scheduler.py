"""私聊主动调度器。

一轮只做一件事：从候选里挑一个人，检查、生成、发送、记账。发出去的
消息是稀缺的注意力资源，所以这里的每一道检查都宁可不发——错过一次
“想起你”最多是少一句话，连着发错则会把人推开。

失败的处理原则：

- 检查没过 → 写 `skipped` 日志，换下一个人；
- 生成失败 → 写 `skipped`，**这一轮就不再试了**（模型或网络出问题，
  换个人也一样）；
- 发送失败 → 写 `failed`，不刷新 `last_proactive_at`、不改待续话题、
  不重试（重试留给下一个 tick）。
"""

from __future__ import annotations

import base64
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.channels.qq import group_allowlist
from app.channels.qq.group_policy import default_policy, load_policy
from app.memory import db as memory_db
from app.proactive.candidates import pick_thought
from app.proactive.candidates import candidates as list_candidates
from app.proactive.checks import (
    in_quiet_hours,
    is_repetitive,
    over_limit,
    recently_active,
    too_soon,
)
from app.proactive.message import ProactiveWriter
from app.proactive.pending import mark_topic_sent, open_topic
from app.proactive.settle import settle_ignores
from app.qq_whitelist import is_whitelisted
from app.proactive.store import (
    last_sent_at,
    record_proactive,
    record_proactive_event,
    recent_channel_events,
    recent_thoughts,
    sent_count_since,
    touch_last_proactive,
)
from app.relationship.appraisal_budget import day_start_iso
from app.relationship.context import render_state_context
from app.relationship.state import closeness_for, now_iso, parse_iso
from app.stickers import files as sticker_files
from app.stickers import selection as sticker_selection

logger = logging.getLogger(__name__)

# 一个 tick 最多考虑几个候选人。候选已按基础分排序，看前几名就够——
# 一轮最多发一条，后面的候选只有在前面全被跳过时才会用到。
_MAX_CANDIDATES = 5

# 群话题的回看窗口：24 小时内的发言才算“现在群里在聊的”。
_GROUP_TOPIC_HOURS = 24
_GROUP_TOPIC_LIMIT = 10

# 只有这几类念头有机会“只发一张表情”。followup 需要文字承接话题，
# care 通常需要安慰的话，都不在这里。
STICKER_KINDS = {"missing", "companionship", "spontaneous"}


@dataclass(frozen=True, slots=True)
class SendResult:
    """这一轮对某个人做的决定，给日志和测试看。"""

    target: str
    channel: str
    kind: str
    status: str  # sent | skipped | failed
    reason: str
    format: str = "text"


class ProactiveScheduler:
    def __init__(
        self,
        settings: Any,
        *,
        sender: Any,
        credits: Any,
        writer: Any = None,
        persona: Any = None,
        now_fn: Any = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.sender = sender
        self.credits = credits
        self.writer = writer or ProactiveWriter()
        self._persona = persona
        self._now = now_fn or now_iso
        self._rng = rng or random.Random()

    def _load_persona(self) -> Any:
        if self._persona is not None:
            return self._persona
        from app.persona.store import get_persona_store

        return get_persona_store().load_active()

    async def run_once(self, now: str | None = None) -> list[SendResult]:
        """跑一轮：先结算旧消息，再决定这一轮发不发。"""
        from app.runtime_settings import get_runtime_store

        stamp = now or self._now()
        runtime = get_runtime_store().load()
        if not runtime.proactive_enabled:
            return []

        persona = self._load_persona()
        dynamics = persona.dynamics
        timezone_name = self.settings.quota_timezone

        conn = memory_db.connect()
        try:
            memory_db.init_schema(conn)
            try:
                settle_ignores(
                    conn,
                    persona_key=persona.state_key,
                    dynamics=dynamics,
                    now=stamp,
                    timezone_name=timezone_name,
                )
            except Exception:  # noqa: BLE001 - 结算失败不该挡发送
                logger.warning("主动消息结算失败", exc_info=True)
            results = await self._private_pass(
                conn, persona=persona, runtime=runtime, now=stamp
            )
            results.extend(
                await self._group_pass(
                    conn, persona=persona, runtime=runtime, now=stamp
                )
            )
            return results
        finally:
            conn.close()

    async def _private_pass(
        self,
        conn: sqlite3.Connection,
        *,
        persona: Any,
        runtime: Any,
        now: str,
    ) -> list[SendResult]:
        dynamics = persona.dynamics
        persona_key = persona.state_key
        timezone_name = self.settings.quota_timezone
        day_start = day_start_iso(now, timezone_name)
        results: list[SendResult] = []

        try:
            cands = list_candidates(
                conn,
                persona_key=persona_key,
                dynamics=dynamics,
                think_threshold=runtime.proactive_think_threshold,
                speak_threshold=runtime.proactive_speak_threshold,
                now=now,
                timezone_name=timezone_name,
            )[:_MAX_CANDIDATES]
        except Exception:  # noqa: BLE001 - 候选计算失败就这轮不发
            logger.warning("主动消息候选计算失败", exc_info=True)
            return results

        for cand in cands:
            channel = f"private:{cand.qq_uin}"

            def skip(reason: str, *, kind: str = "", score: float = 0.0) -> None:
                record_proactive(
                    conn,
                    persona_key=persona_key,
                    target_key=cand.person_key,
                    channel_id=channel,
                    kind=kind,
                    score=score,
                    threshold=runtime.proactive_speak_threshold,
                    status="skipped",
                    reason=reason,
                    now=now,
                )
                results.append(
                    SendResult(
                        cand.person_key, channel, kind, "skipped", reason
                    )
                )

            # QQ 可能已经换绑：绝不能把消息发给新主人。
            # `qq:` 身份本来就没有账号，一旦绑上就说明该按 wp: 走了。
            binding = self.credits.binding_of_qq(cand.qq_uin)
            if cand.person_key.startswith("wp:"):
                if binding is None or binding.user_id != cand.wp_user_id:
                    results.append(
                        SendResult(
                            cand.person_key, channel, "", "skipped", "binding_changed"
                        )
                    )
                    continue
            else:
                if binding is not None:
                    results.append(
                        SendResult(
                            cand.person_key, channel, "", "skipped", "binding_changed"
                        )
                    )
                    continue
                # 名单可能在算出候选人之后被改掉，发送前再确认一次。
                if not is_whitelisted(conn, cand.qq_uin):
                    results.append(
                        SendResult(
                            cand.person_key, channel, "", "skipped", "not_whitelisted"
                        )
                    )
                    continue

            thought = pick_thought(
                cand.thoughts, runtime.proactive_speak_threshold
            )
            if thought is None:
                # 念头不够强烈：这是“想过但没说”，不留日志。
                continue

            # 主动表情：由她自己的状态决定，不设时段 / 间隔 / 每日上限。
            # 没有合适的素材时继续走文字路径。
            if thought.kind in STICKER_KINDS:
                if await self._try_sticker(
                    conn,
                    cand=cand,
                    thought=thought,
                    runtime=runtime,
                    dynamics=dynamics,
                    persona_key=persona_key,
                    channel=channel,
                    now=now,
                    results=results,
                ):
                    break

            if in_quiet_hours(now, dynamics.safety.quiet_hours, timezone_name):
                skip("quiet_hours", kind=thought.kind, score=thought.score)
                continue
            if too_soon(
                cand.state.last_proactive_at,
                now,
                runtime.proactive_min_interval_minutes,
            ):
                skip("min_interval", kind=thought.kind, score=thought.score)
                continue
            if over_limit(
                sent_count_since(
                    conn,
                    persona_key=persona_key,
                    since=day_start,
                    target_key=cand.person_key,
                ),
                runtime.proactive_per_user_daily_limit,
            ):
                skip("daily_limit", kind=thought.kind, score=thought.score)
                continue
            if over_limit(
                sent_count_since(
                    conn, persona_key=persona_key, since=day_start
                ),
                runtime.proactive_global_daily_limit,
            ):
                skip("global_limit", kind=thought.kind, score=thought.score)
                continue
            if recently_active(
                conn,
                person_key=cand.person_key,
                channel_id=channel,
                now=now,
                minutes=dynamics.proactive.recent_active_minutes,
            ):
                skip("recently_active", kind=thought.kind, score=thought.score)
                continue

            topic_row = None
            topic_hint = ""
            if thought.kind == "event_followup":
                topic_row = open_topic(
                    conn,
                    persona_key,
                    cand.person_key,
                    now=now,
                    timezone_name=timezone_name,
                )
                if topic_row is not None:
                    topic_hint = str(topic_row["topic"])

            try:
                composed = await self.writer.compose(
                    persona=persona,
                    kind=thought.kind,
                    state_context=render_state_context(cand.state),
                    recent=list(
                        reversed(
                            recent_channel_events(
                                conn, cand.person_key, channel, limit=6
                            )
                        )
                    ),
                    topic_hint=topic_hint,
                    timeout=dynamics.proactive.compose_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - 生成器本应自己吞掉异常
                logger.warning("主动消息生成异常", exc_info=True)
                skip("compose_error", kind=thought.kind, score=thought.score)
                break

            if composed.status != "ok":
                skip(
                    f"compose_{composed.status}",
                    kind=thought.kind,
                    score=thought.score,
                )
                break

            if is_repetitive(
                composed.text,
                recent_thoughts(conn, persona_key, cand.person_key),
                threshold=dynamics.proactive.repeat_similarity,
            ):
                skip("repetitive", kind=thought.kind, score=thought.score)
                break

            try:
                await self.sender.send_private_message(
                    int(cand.qq_uin), composed.text
                )
            except Exception as exc:  # noqa: BLE001 - 发送失败不重试
                reason = str(exc)[:200]
                record_proactive(
                    conn,
                    persona_key=persona_key,
                    target_key=cand.person_key,
                    channel_id=channel,
                    kind=thought.kind,
                    thought=composed.text,
                    score=thought.score,
                    threshold=runtime.proactive_speak_threshold,
                    status="failed",
                    reason=reason,
                    now=now,
                )
                results.append(
                    SendResult(
                        cand.person_key,
                        channel,
                        thought.kind,
                        "failed",
                        reason,
                    )
                )
                break

            event_id = record_proactive_event(
                conn,
                person_key=cand.person_key,
                channel_id=channel,
                scope="personal",
                audience=[cand.person_key],
                content=composed.text,
                now=now,
            )
            record_proactive(
                conn,
                persona_key=persona_key,
                target_key=cand.person_key,
                channel_id=channel,
                kind=thought.kind,
                thought=composed.text,
                score=thought.score,
                threshold=runtime.proactive_speak_threshold,
                status="sent",
                event_id=event_id,
                now=now,
            )
            touch_last_proactive(
                conn, persona_key, cand.person_key, now=now
            )
            if topic_row is not None:
                mark_topic_sent(conn, topic_row["id"], now=now)

            results.append(
                SendResult(cand.person_key, channel, thought.kind, "sent", "")
            )
            # 一轮只发一条：这一轮的成功就是下一轮的截止。
            break

        return results

    def _sticker_urge(self, state: Any, kind: str, dynamics: Any) -> float:
        """她这一刻有多想发个表情：亲近、心情、想念各占一点。"""
        closeness = closeness_for(float(state.affection), dynamics)
        urge = (
            0.12
            + 0.35 * closeness
            + 0.25 * max(float(state.valence), 0.0)
            + 0.15 * min(float(state.missing) / 100.0, 1.0)
        )
        urge += 0.10 if kind == "missing" else 0.05
        return max(0.0, min(0.95, urge))

    async def _try_sticker(
        self,
        conn: sqlite3.Connection,
        *,
        cand: Any,
        thought: Any,
        runtime: Any,
        dynamics: Any,
        persona_key: str,
        channel: str,
        now: str,
        results: list[SendResult],
    ) -> bool:
        """尝试只发一张表情；返回 True 表示这一轮已经处理完（发送或失败）。

        返回 False 表示“没有这个念头/没有合适素材”，调用方继续走文字。
        主动表情不检查 quiet_hours / 间隔 / 每日上限——这是产品决定：
        发不发只取决于她自己的意愿。
        """
        if self._rng.random() > self._sticker_urge(cand.state, thought.kind, dynamics):
            return False

        tones = sticker_selection.tones_for_state(
            str(cand.state.conflict_stage), float(cand.state.valence)
        )
        sticker = None
        for tone in tones:
            sticker = sticker_selection.pick_for_thought(
                conn,
                tone=tone,
                kind=thought.kind,
                target_key=cand.person_key,
                rng=self._rng,
            )
            if sticker is not None:
                break
        if sticker is None:
            return False

        try:
            data = sticker_files.read_bytes(sticker.file_name)
        except (OSError, ValueError):
            logger.warning("主动表情文件读不到，回退文字 sticker=%s", sticker.id)
            return False

        segment = {
            "type": "image",
            "data": {
                "file": "base64://" + base64.b64encode(data).decode("ascii"),
            },
        }
        sender = getattr(self.sender, "send_private_segments", None)
        if sender is None:
            return False

        try:
            await sender(int(cand.qq_uin), [segment])
        except Exception as exc:  # noqa: BLE001 - 发送失败不重试
            reason = str(exc)[:200]
            record_proactive(
                conn,
                persona_key=persona_key,
                target_key=cand.person_key,
                channel_id=channel,
                kind=thought.kind,
                score=thought.score,
                threshold=runtime.proactive_speak_threshold,
                status="failed",
                reason=reason,
                format="sticker",
                sticker_id=sticker.id,
                now=now,
            )
            results.append(
                SendResult(
                    cand.person_key,
                    channel,
                    thought.kind,
                    "failed",
                    reason,
                    format="sticker",
                )
            )
            return True

        content = f"[表情包：{sticker.description or sticker.file_name}]"[:500]
        event_id = record_proactive_event(
            conn,
            person_key=cand.person_key,
            channel_id=channel,
            scope="personal",
            audience=[cand.person_key],
            content=content,
            now=now,
        )
        record_proactive(
            conn,
            persona_key=persona_key,
            target_key=cand.person_key,
            channel_id=channel,
            kind=thought.kind,
            thought=content,
            score=thought.score,
            threshold=runtime.proactive_speak_threshold,
            status="sent",
            event_id=event_id,
            format="sticker",
            sticker_id=sticker.id,
            now=now,
        )
        touch_last_proactive(conn, persona_key, cand.person_key, now=now)
        results.append(
            SendResult(
                cand.person_key,
                channel,
                thought.kind,
                "sent",
                "",
                format="sticker",
            )
        )
        return True

    async def _group_pass(
        self,
        conn: sqlite3.Connection,
        *,
        persona: Any,
        runtime: Any,
        now: str,
    ) -> list[SendResult]:
        """群主动：每个 tick 最多在一个群里开口。

        群主动不看关系、不看念头分数——群里有人在聊就是由头。只受
        “这个群允许不允许”和频率限制约束，而且**不写任何人的关系状态**：
        她在群里说一句话，不等于对某个成员更亲近。
        """
        dynamics = persona.dynamics
        persona_key = persona.state_key
        timezone_name = self.settings.quota_timezone
        day_start = day_start_iso(now, timezone_name)
        fallback = default_policy(self.settings.qq_trigger)
        results: list[SendResult] = []

        # 名单以运行时表为准（后台增删立刻生效），环境变量只在首次播种时用。
        allowed = group_allowlist.allowed_group_ids(
            conn, env_groups=self.settings.qq_allowed_group_ids
        )
        for gid in sorted(allowed):
            target = f"group:{gid}"

            def skip(reason: str, *, kind: str = "spontaneous") -> None:
                record_proactive(
                    conn,
                    persona_key=persona_key,
                    target_key=target,
                    channel_id=target,
                    kind=kind,
                    status="skipped",
                    reason=reason,
                    now=now,
                )
                results.append(
                    SendResult(target, target, kind, "skipped", reason)
                )

            policy = load_policy(conn, gid, fallback=fallback)
            if not policy.proactive:
                # 明确关掉的群连日志都不写：这是配置，不是一次决定。
                results.append(
                    SendResult(
                        target, target, "", "skipped", "policy_disabled"
                    )
                )
                continue

            if in_quiet_hours(
                now, dynamics.safety.quiet_hours, timezone_name
            ):
                skip("quiet_hours")
                continue
            if too_soon(
                last_sent_at(conn, target_key=target),
                now,
                runtime.proactive_group_interval_minutes,
            ):
                skip("min_interval")
                continue
            if over_limit(
                sent_count_since(
                    conn,
                    persona_key=persona_key,
                    since=day_start,
                    target_key=target,
                ),
                runtime.proactive_group_daily_limit,
            ):
                skip("daily_limit")
                continue

            try:
                since = (
                    parse_iso(now) - timedelta(hours=_GROUP_TOPIC_HOURS)
                ).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                since = ""
            rows = conn.execute(
                """SELECT content FROM events
                    WHERE channel_id = ? AND source = 'user_message'
                      AND occurred_at >= ?
                    ORDER BY id DESC LIMIT ?""",
                (target, since, _GROUP_TOPIC_LIMIT),
            ).fetchall()
            topics = [row["content"] for row in rows if row["content"]]
            if not topics:
                skip("no_topic")
                continue
            # 查询是新→旧，拼进提示词时按时间正序读起来更自然。
            group_topic = "\n".join(reversed(topics))

            try:
                composed = await self.writer.compose(
                    persona=persona,
                    kind="spontaneous",
                    state_context="",
                    recent=[],
                    topic_hint="",
                    channel="group",
                    group_topic=group_topic,
                    timeout=dynamics.proactive.compose_timeout_seconds,
                )
            except Exception:  # noqa: BLE001 - 生成器本应自己吞掉异常
                logger.warning("群主动消息生成异常", exc_info=True)
                skip("compose_error")
                break
            if composed.status != "ok":
                skip(f"compose_{composed.status}")
                break

            if is_repetitive(
                composed.text,
                recent_thoughts(conn, persona_key, target),
                threshold=dynamics.proactive.repeat_similarity,
            ):
                skip("repetitive")
                break

            try:
                await self.sender.send_group_message(int(gid), composed.text)
            except Exception as exc:  # noqa: BLE001 - 发送失败不重试
                reason = str(exc)[:200]
                record_proactive(
                    conn,
                    persona_key=persona_key,
                    target_key=target,
                    channel_id=target,
                    kind="spontaneous",
                    thought=composed.text,
                    status="failed",
                    reason=reason,
                    now=now,
                )
                results.append(
                    SendResult(
                        target, target, "spontaneous", "failed", reason
                    )
                )
                break

            event_id = record_proactive_event(
                conn,
                person_key=target,
                channel_id=target,
                scope="group",
                audience=[target],
                content=composed.text,
                now=now,
            )
            record_proactive(
                conn,
                persona_key=persona_key,
                target_key=target,
                channel_id=target,
                kind="spontaneous",
                thought=composed.text,
                status="sent",
                event_id=event_id,
                now=now,
            )
            results.append(
                SendResult(target, target, "spontaneous", "sent", "")
            )
            # 每个 tick 最多在一个群里开口。
            break

        return results
