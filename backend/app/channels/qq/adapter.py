"""OneBot 事件 → 内部 ChatMessage → agent → 群消息。

设计原则
--------
1. **默认拒绝**：`QQ_ALLOWED_GROUPS` 为空时，任何群消息都不处理。
2. **只处理文本**：群聊与私聊都支持；频道、图片、表情先忽略。
3. **用户内容只是数据**：群消息会作为 `ChatMessage(role="user")` 进入模型，
   永远不会拼进 system prompt，也不会改变管理员身份。
4. **幂等**：NapCat 在网络抖动时可能重发同一个 `message_id`，必须去重。
5. **失败可观察**：发送失败只记日志，不让后台任务崩掉整个进程。

这一层刻意保持“薄”：只读绑定关系，不写长期记忆，也从不主动发消息。

消息处理顺序（顺序本身就是规则）：

    去重 → 触发判断 → 斜杠命令 → 绑定判定 → 额度 → agent

命令在绑定判定之前：命令自己对自己的权限负责（比如 /绑定 只在私聊执行），
也不消耗额度；未绑定的人在命令里拿到的是引导，而不是 AI 回答。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from typing import Any

from app.agent.core import stream_reply
from app.channels.qq import commands, group_allowlist
from app.channels.qq.client import OneBotError, OneBotTransport
from app.channels.qq.commands import CommandContext, dispatch, parse_command
from app.channels.qq.conversation import ConversationCoordinator
from app.channels.qq.group_policy import (
    GroupPolicy,
    TriggerMatch,
    default_policy,
    load_policy,
    match_trigger,
)
from app.channels.qq.pacing import bubble_delay, segment_bubbles
from app.config import Settings
from app.credits import CreditStore, get_credit_store
from app.memory import db as memory_db
from app.memory import pipeline
from app.persona.models import PersonaVersion
from app.persona.store import get_persona_store
from app.proactive import pending as pending_topic
from app.proactive.checks import too_soon
from app.proactive.store import last_sent_at
from app.qq_quota import QQQuotaGate, QQUsage, get_qq_quota_gate
from app.relationship.appraisal import (
    AppraisalCall,
    apply_appraisal,
    record_appraisal_log,
)
from app.relationship.appraisal_budget import local_day, reserve_budget
from app.relationship.context import render_group_state_context, render_state_context
from app.relationship.decision import Decision, decide, record_decision
from app.relationship.group_turn import (
    decide_group,
    load_group_signals,
    recent_speaker_texts,
)
from app.relationship.state import _zone, now_iso, parse_iso
from app.relationship.turn import (
    load_signals,
    record_attention,
    record_reply,
    record_silence,
    relationship_subject,
)
from app.runtime_settings import get_runtime_store
from app.schemas import ChatMessage

logger = logging.getLogger(__name__)

# 一条 QQ 文本消息最多发多少字符。太长会刷屏，也容易被平台限流。
# 这里不是模型的输出上限，而是“单条群消息”的展示上限；超出的部分拆成多条。
MAX_MESSAGE_CHARS = 1_200

# 去重表最多记多少个 message_id。QQ 群消息量不大，500 足够覆盖重发窗口。
MAX_SEEN_MESSAGES = 500

# 群主动消息发出后 12 小时内有人在群里说话，说明“开口没白费”。
# 这段时间里的发言给关系状态记一次中等注意力，置信度 0.6：
# 比私聊低（群里的热闹不一定冲着她），但也足以抵消一次“被忽略”的扣分。
GROUP_ATTENTION_WINDOW_MINUTES = 12 * 60
GROUP_ATTENTION_CONFIDENCE = 0.6


@dataclass(frozen=True, slots=True)
class Incoming:
    """解析后的入站消息。私聊的 group_id 固定为 0。"""

    message_type: str  # "group" | "private"
    group_id: int
    user_id: int
    self_id: int
    text: str
    at_self: bool
    message_id: int | None


class QQAdapter:
    """处理 NapCat 推送的 OneBot 事件。"""

    def __init__(
        self,
        settings: Settings,
        sender: OneBotTransport,
        *,
        stream: Callable[..., AsyncIterator[dict[str, Any]]] = stream_reply,
        credits: CreditStore | None = None,
        gate: QQQuotaGate | None = None,
        persona: PersonaVersion | None = None,
        rng: random.Random | None = None,
        coordinator: ConversationCoordinator | None = None,
        appraiser: Any | None = None,
    ) -> None:
        self.settings = settings
        self.sender = sender
        self._stream = stream
        # 绑定关系存在额度库里。可注入是为了测试用一个临时数据库，
        # 生产不传时惰性取全局实例（导入时就建库会让 --help 之类也写文件）。
        self.credits = credits
        # 额度门面同理：测试注入固定参数的门面，生产惰性取全局实例。
        self.gate = gate

        # 人设由 agent 自己加载；适配器也需要同一份 dynamics 做概率和节奏。
        # 注入是为了测试可以直接给一份零防抖、固定概率的人设。
        self._injected_persona = persona

        # 概率采样和气泡切分的随机源。固定种子能让测试复现同一次掷骰子。
        self._rng = rng or random.Random()

        # 私聊防抖/打断状态。群聊仍走原来的同步路径。
        self._coordinator = coordinator or ConversationCoordinator()

        # 每轮私聊的同步模型评估。默认 None：不评估、不花钱，
        # 现有测试和离线运行都不会误调真实模型；生产入口显式注入 Appraiser。
        self._appraiser = appraiser

        # (群号, QQ 号) -> 最近几轮消息。只在内存里，进程重启就清空。
        # 这是有意的：在账号绑定/关系记忆做好之前，不假装自己有长期记忆。
        self._history: dict[tuple[int, int], list[ChatMessage]] = {}

        # (群号, message_id) -> 时间戳。OrderedDict 的插入顺序帮助我们淘汰最旧的记录。
        # 不用单独的 message_id 做 key：不同群的 ID 理论上可能重复，
        # 只按 ID 去重会误吞另一个群的消息。
        self._seen: OrderedDict[tuple[int, int], float] = OrderedDict()

        # 同一个群同一个人连续发消息时，串行处理，避免历史顺序错乱。
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}

        trigger = self.settings.qq_trigger.strip().lower()
        if trigger not in {"at", "prefix", "all"}:
            logger.warning("未知的 QQ_TRIGGER=%r，回退到 at", trigger)
            trigger = "at"
        self._trigger = trigger

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------
    async def handle_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理一个 OneBot 事件，返回处理结果（主要给测试/日志用）。

        真正的 HTTP 路由会把它放进后台任务，先快速返回 204，
        因为 NapCat 不关心我们的回复内容，只关心有没有及时收到响应。
        """
        incoming = self._parse_message(payload)
        if incoming is None:
            return {"handled": False, "reason": "not_a_text_message"}

        # 机器人自己发的消息（包括 NapCat 回显）不处理，否则会自己和自己聊天。
        if incoming.self_id and incoming.user_id == incoming.self_id:
            return {"handled": False, "reason": "self_message"}

        if incoming.message_type == "group":
            if incoming.group_id not in await self._allowed_group_ids():
                return {"handled": False, "reason": "group_not_allowed"}
            policy: GroupPolicy | None = self._group_policy(incoming.group_id)
            match = self._match_trigger(
                incoming.text, incoming.at_self, trigger=policy.trigger
            )
        else:
            # 私聊没有 @ 的概念，整条文本就是正文。
            policy = None
            match = TriggerMatch(incoming.text.strip(), False)

        if self._is_duplicate(incoming.group_id, incoming.message_id):
            return {"handled": False, "reason": "duplicate_message"}

        content = match.content

        is_private = incoming.message_type == "private"
        key = (incoming.group_id, incoming.user_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            store = self.credits or get_credit_store()
            admin_whitelisted = incoming.user_id in self.settings.qq_admin_user_ids
            binding = store.binding_of_qq(str(incoming.user_id))
            is_admin = admin_whitelisted or (binding.is_admin if binding else False)

            # 0. 群消息没叫到梅林：不回，但把原话记进时间线。
            #    “她当时没接话”本身是事实，之后的待续话题和主动消息要用。
            if not content:
                if incoming.message_type == "group" and incoming.text.strip():
                    await asyncio.to_thread(
                        pipeline.record_qq_ignored,
                        incoming.user_id,
                        incoming.group_id,
                        binding,
                        incoming.text,
                    )
                return {"handled": False, "reason": "not_triggered_or_empty"}

            # 1. 斜杠命令：先于绑定判定，永不进 agent、不进历史。
            parsed = parse_command(content)
            if parsed is not None:
                reply = dispatch(
                    parsed,
                    CommandContext(
                        qq_uin=incoming.user_id,
                        is_private=is_private,
                        group_id=None if is_private else incoming.group_id,
                        args="",
                        bound_user_id=binding.user_id if binding else None,
                        is_admin=is_admin,
                        store=store,
                    ),
                )
                return await self._send(incoming, reply, reason="command")

            # 2. 未绑定：只回引导。管理员白名单是自救通道，不受绑定限制。
            if binding is None and not admin_whitelisted:
                guide = (
                    commands.private_guide(self.settings.wp_site_url)
                    if is_private
                    else commands.GROUP_GUIDE
                )
                return await self._send(incoming, guide, reason="guide_unbound")

            # 3. 群聊：silent 策略只记录不说话；被指名的消息按关系状态
            #    掷一次骰子（概率有群下限）；普通闲聊直接回复，不进概率模型。
            #    防抖/多气泡仍只作用于私聊。
            if not is_private:
                # 刚在群里主动说过话，现在有人接话：这是“她在场”的证据。
                # 只影响这个人自己的关系状态，不影响回不回。
                if binding is not None:
                    try:
                        await asyncio.to_thread(
                            self._record_group_attention, incoming, binding
                        )
                    except Exception:  # noqa: BLE001 - 注意力记账不该挡聊天
                        logger.warning(
                            "群注意力记录失败 group=%s",
                            incoming.group_id,
                            exc_info=True,
                        )
                if policy is not None and policy.reply_policy == "silent":
                    await asyncio.to_thread(
                        pipeline.record_qq_ignored,
                        incoming.user_id,
                        incoming.group_id,
                        binding,
                        content,
                    )
                    return {"handled": True, "reason": "silent"}
                if binding is not None and match.addressed:
                    return await self._addressed_group_round(
                        incoming,
                        content,
                        binding=binding,
                        is_admin=is_admin,
                    )
                return await self._run_chat_turn(
                    incoming,
                    content,
                    binding=binding,
                    is_admin=is_admin,
                    is_private=False,
                )

        # 4. 私聊：进入防抖协调器。命令和引导已经在上面即时处理；
        # 正文会等一小段时间，把连发的几条合并成一轮再决策。
        persona = self._persona()
        pacing = persona.dynamics.pacing
        return await self._coordinator.submit(
            key,
            content,
            handler=lambda texts: self._private_round(
                incoming,
                key,
                texts,
                store=store,
                binding=binding,
                is_admin=is_admin,
                persona=persona,
            ),
            debounce=pacing.debounce_seconds,
            max_debounce=pacing.debounce_max_seconds,
        )

    # ------------------------------------------------------------------
    # 群聊回合
    # ------------------------------------------------------------------
    async def _addressed_group_round(
        self,
        incoming: Incoming,
        content: str,
        *,
        binding: object | None,
        is_admin: bool,
    ) -> dict[str, Any]:
        """被指名的群消息：读状态 → 掷骰子 → 回不回。

        和私聊不同，这里**不**记 `record_silence`，回复后也**不**写关系：
        群聊只是“在场说了句话”，不积累回复债、不涨好感。
        """
        persona = self._persona()
        decisions = await asyncio.to_thread(
            self._group_decide_and_record,
            incoming,
            content,
            binding=binding,
            persona=persona,
        )

        if decisions is not None:
            decision, state = decisions
            if not decision.reply:
                await asyncio.to_thread(
                    pipeline.record_qq_ignored,
                    incoming.user_id,
                    incoming.group_id,
                    binding,
                    content,
                )
                return {"handled": True, "reason": "group_silent", "reply": ""}
            state_context = render_group_state_context(state)
        else:
            # 关系库不可用：fail-open，照常回复，不写决策。
            state_context = None

        return await self._run_chat_turn(
            incoming,
            content,
            binding=binding,
            is_admin=is_admin,
            is_private=False,
            state_context=state_context,
        )

    def _group_decide_and_record(
        self,
        incoming: Incoming,
        content: str,
        *,
        binding: object | None,
        persona: PersonaVersion,
    ) -> tuple[Decision, Any] | None:
        """整段数据库操作放在同一个线程里做完，返回 `(决策, 掷骰子前状态)`。

        连接不能跨线程传递，所以不能在事件循环里开连接再丢进线程池。
        任何异常都向上抛，由调用方 fail-open。
        """
        dynamics = persona.dynamics
        channel_id = f"group:{incoming.group_id}"
        conn = None
        try:
            conn = memory_db.connect()
            memory_db.init_schema(conn)
            person_key, role, signals = load_group_signals(
                conn,
                persona.state_key,
                incoming.user_id,
                getattr(binding, "user_id", None),
                dynamics=dynamics,
                timezone_name=self.settings.quota_timezone,
            )
            recent = recent_speaker_texts(conn, person_key, channel_id, 5)
            decision = decide_group(
                text=content,
                recent=recent,
                signals=signals,
                dynamics=dynamics,
                rng=self._rng,
                name=persona.name,
            )
            record_decision(
                conn,
                persona_key=persona.state_key,
                channel_id=channel_id,
                person_key=person_key,
                message_id=incoming.message_id,
                decision=decision,
                rule_score=decision.rule_willingness,
            )
            return decision, signals.state
        except Exception:  # noqa: BLE001 - 判断层坏了不能表现成“她不理人”
            logger.warning(
                "群聊关系判断失败 group=%s，按照常回复处理",
                incoming.group_id,
                exc_info=True,
            )
            return None
        finally:
            if conn is not None:
                conn.close()

    # ------------------------------------------------------------------
    # 私聊回合
    # ------------------------------------------------------------------
    def _record_group_attention(
        self, incoming: Incoming, binding: Any
    ) -> None:
        """群主动后 12 小时内的群发言，给说话人记一次中等注意力。

        只在“最近在这个群主动说过话”时写：没人开口的群，成员说话和
        梅林没有关系，不该被当成对她的回应。
        """
        target = f"group:{incoming.group_id}"
        conn = None
        try:
            conn = memory_db.connect()
            memory_db.init_schema(conn)
            last_at = last_sent_at(conn, target_key=target)
            if last_at is None or not too_soon(
                last_at, now_iso(), GROUP_ATTENTION_WINDOW_MINUTES
            ):
                return
            persona = self._persona()
            person_key, role = relationship_subject(
                conn, incoming.user_id, getattr(binding, "user_id", None)
            )
            record_attention(
                conn,
                persona.state_key,
                person_key,
                role=role,
                dynamics=persona.dynamics,
                confidence=GROUP_ATTENTION_CONFIDENCE,
                timezone_name=self.settings.quota_timezone,
            )
        finally:
            if conn is not None:
                conn.close()

    def _persona(self) -> PersonaVersion:
        """当前人设；每轮重新加载，改了覆盖层不必重启。

        人设文件很小，一次私聊回合加载一次的成本可以忽略；换来的是
        管理员调整 `dynamics.override.json` 后下一轮立刻生效。加载失败时
        退化成纯默认 dynamics，不阻塞聊天。
        """
        if self._injected_persona is not None:
            return self._injected_persona
        try:
            return get_persona_store().load_active()
        except Exception:  # noqa: BLE001 - 人设坏了 agent 也会报错，这里只保证决策能跑
            logger.warning("QQ 适配器加载人设失败，使用默认 dynamics", exc_info=True)
            return PersonaVersion(
                schema="l2d.persona/v1",
                id=self.settings.persona_id,
                version=1,
                name=self.settings.persona_id,
                core="（人设文件不可用，仅使用默认参数）",
            )

    async def _private_round(
        self,
        incoming: Incoming,
        key: tuple[int, int],
        texts: list[str],
        *,
        store: CreditStore,
        binding: object | None,
        is_admin: bool,
        persona: PersonaVersion,
    ) -> dict[str, Any]:
        """处理一轮合并后的私聊：读状态 → 评估 → 掷骰子 → 决定是否回复。"""
        content = "\n".join(texts)
        dynamics = persona.dynamics
        crisis = any(
            word and word in content for word in dynamics.safety.crisis_keywords
        )

        loaded = await asyncio.to_thread(
            self._load_private_signals, incoming, binding, persona
        )
        if loaded is None:
            # 关系库不可用：fail-open，直接回复，不写关系。
            return await self._run_chat_turn(
                incoming,
                content,
                binding=binding,
                is_admin=is_admin,
                is_private=True,
            )
        person_key, role, signals = loaded

        runtime = get_runtime_store().load()
        policy = self.settings.qq_private_policy
        call: AppraisalCall | None = None
        if (
            runtime.appraisal_enabled
            and self._appraiser is not None
            and policy != "silent"
            and not crisis
        ):
            if await asyncio.to_thread(self._reserve_appraisal, person_key):
                call = await self._appraise_private(
                    incoming,
                    texts,
                    person_key=person_key,
                    signals=signals,
                    persona=persona,
                )
            else:
                await asyncio.to_thread(
                    self._log_appraisal_skip, incoming, person_key, runtime
                )

        decided = await asyncio.to_thread(
            self._decide_and_record,
            incoming,
            texts,
            binding=binding,
            persona=persona,
            person_key=person_key,
            role=role,
            signals=signals,
            call=call,
        )
        decision, state = decided

        if decision is not None and not decision.reply:
            # 沉默是合法结果：不占额度、不调模型，但用户这条仍进时间线。
            await asyncio.to_thread(
                pipeline.record_qq_ignored,
                incoming.user_id,
                None,
                binding,
                content,
            )
            return {"handled": True, "reason": "silent", "reply": ""}

        # 决策层出错时 decision 为 None，按“照常回复”处理（fail-open）。
        relationship = (
            (persona.state_key, person_key, role) if decision is not None else None
        )
        appraisal_ok = (
            call is not None
            and call.status == "ok"
            and call.result is not None
            and call.result.confidence >= dynamics.appraisal.confidence_min
        )
        return await self._run_chat_turn(
            incoming,
            content,
            binding=binding,
            is_admin=is_admin,
            is_private=True,
            relationship=relationship,
            dynamics=dynamics,
            state_context=render_state_context(state) if state is not None else None,
            baseline_affection=not appraisal_ok,
        )

    def _load_private_signals(
        self,
        incoming: Incoming,
        binding: object | None,
        persona: PersonaVersion,
    ) -> tuple[str, str, Any] | None:
        """读取关系状态；失败返回 None，由调用方 fail-open。"""
        conn = None
        try:
            conn = memory_db.connect()
            memory_db.init_schema(conn)
            bound_user_id = getattr(binding, "user_id", None) if binding else None
            person_key, role = relationship_subject(
                conn, incoming.user_id, bound_user_id
            )
            signals = load_signals(
                conn,
                persona.state_key,
                person_key,
                role=role,
                dynamics=persona.dynamics,
                timezone_name=self.settings.quota_timezone,
            )
            return person_key, role, signals
        except Exception:  # noqa: BLE001 - 决策失败不能让私聊变哑
            logger.warning(
                "QQ 私聊读取关系状态失败 qq=%s", incoming.user_id, exc_info=True
            )
            return None
        finally:
            if conn is not None:
                conn.close()

    async def _appraise_private(
        self,
        incoming: Incoming,
        texts: list[str],
        *,
        person_key: str,
        signals: Any,
        persona: PersonaVersion,
    ) -> AppraisalCall | None:
        """调用一次轻量评估。异常已经由 Appraiser 内部吞掉。"""
        if self._appraiser is None:
            return None
        key = (incoming.group_id, incoming.user_id)
        recent = [message.content for message in self._history.get(key, [])[-4:]]
        local_time = parse_iso(now_iso()).astimezone(
            _zone(self.settings.quota_timezone)
        ).strftime("%Y-%m-%d %H:%M")
        recent_events = await asyncio.to_thread(
            self._recent_appraisal_reasons, persona.state_key, person_key
        )
        runtime = get_runtime_store().load()
        try:
            return await self._appraiser.appraise(
                persona_name=persona.name,
                user_text="\n".join(texts),
                recent=recent,
                state_context=render_state_context(signals.state),
                timeout=runtime.appraisal_timeout_seconds,
                local_time=local_time,
                recent_events=recent_events,
            )
        except Exception:  # noqa: BLE001 - 评估是增强，不能阻断回复
            logger.warning(
                "QQ 关系评估失败 qq=%s", incoming.user_id, exc_info=True
            )
            return None

    def _recent_appraisal_reasons(
        self, persona_key: str, person_key: str
    ) -> list[str]:
        """取最近几条评估理由，给本轮评估一点“刚才发生过什么”的上下文。"""
        conn = None
        try:
            conn = memory_db.connect()
            memory_db.init_schema(conn)
            rows = conn.execute(
                """
                SELECT reason FROM appraisal_log
                 WHERE persona_key = ? AND person_key = ?
                   AND reason IS NOT NULL AND reason != ''
                 ORDER BY id DESC LIMIT 3
                """,
                (persona_key, person_key),
            ).fetchall()
            return [str(row["reason"]) for row in reversed(rows)]
        except Exception:  # noqa: BLE001
            return []
        finally:
            if conn is not None:
                conn.close()

    def _reserve_appraisal(self, person_key: str) -> bool:
        """占一次评估预算；出错时放行（评估是增强，不是可用性前提）。"""
        conn = None
        try:
            runtime = get_runtime_store().load()
            conn = memory_db.connect()
            memory_db.init_schema(conn)
            day = local_day(None, self.settings.quota_timezone)
            return reserve_budget(
                conn,
                person_key=person_key,
                day=day,
                global_limit=runtime.appraisal_global_daily_budget,
                user_limit=runtime.appraisal_per_user_daily_budget,
            )
        except Exception:  # noqa: BLE001
            logger.warning("评估预算占位失败，本次放行", exc_info=True)
            return True
        finally:
            if conn is not None:
                conn.close()

    def _log_appraisal_skip(
        self, incoming: Incoming, person_key: str, runtime: Any
    ) -> None:
        """预算用尽时留一行日志，不发模型调用。"""
        conn = None
        try:
            conn = memory_db.connect()
            memory_db.init_schema(conn)
            record_appraisal_log(
                conn,
                persona_key=self._persona().state_key,
                person_key=person_key,
                message_id=(
                    None
                    if incoming.message_id is None
                    else str(incoming.message_id)
                ),
                result=None,
                status="over_budget",
                skipped_reason="over_budget",
            )
        except Exception:  # noqa: BLE001
            logger.warning("记录评估预算日志失败", exc_info=True)
        finally:
            if conn is not None:
                conn.close()

    def _decide_and_record(
        self,
        incoming: Incoming,
        texts: list[str],
        *,
        binding: object | None,
        persona: PersonaVersion,
        person_key: str,
        role: str,
        signals: Any,
        call: AppraisalCall | None,
    ) -> tuple[Decision | None, Any]:
        """掷骰子并落账；任何异常返回 `(None, None)` 让调用方 fail-open。

        返回值里的状态是**掷骰子前**的快照：本轮语气用它，评估增量
        只影响下一轮，避免模型在同一轮里又当裁判又当选手。
        """
        content = "\n".join(texts)
        dynamics = persona.dynamics
        policy = self.settings.qq_private_policy
        crisis = any(
            word and word in content for word in dynamics.safety.crisis_keywords
        )

        conn = None
        try:
            conn = memory_db.connect()
            memory_db.init_schema(conn)

            if policy == "silent" and not crisis:
                decision = Decision(
                    willingness=0.0,
                    affinity=signals.affinity,
                    valence=signals.state.valence,
                    energy=signals.state.energy,
                    reply_debt=signals.state.reply_debt,
                    p0=0.0,
                    p=0.0,
                    roll=None,
                    reply=False,
                    reason="policy_silent",
                    conflict_stage=signals.state.conflict_stage,
                )
            else:
                appraisal = call.result if call and call.status == "ok" else None
                decision = decide(
                    text=content,
                    recent=texts[:-1],
                    affinity=signals.affinity,
                    valence=signals.state.valence,
                    energy=signals.state.energy,
                    reply_debt=signals.state.reply_debt,
                    patience=signals.state.patience,
                    security=signals.state.security,
                    dynamics=dynamics,
                    rng=self._rng,
                    force_reply=(policy == "always" or crisis),
                    reason="crisis" if crisis else ("policy_always" if policy == "always" else ""),
                    name=persona.name,
                    reply_bias=appraisal.reply_bias if appraisal else 0.0,
                    bias_confidence=appraisal.confidence if appraisal else 0.0,
                    appraisal_event=appraisal.event if appraisal else None,
                    stage=signals.state.conflict_stage,
                )

            record_decision(
                conn,
                persona_key=persona.state_key,
                channel_id="qq_private",
                person_key=person_key,
                message_id=incoming.message_id,
                decision=decision,
                rule_score=decision.rule_willingness,
            )
            # 用户这一轮确实开口了，无论回不回都算“刚聊过”。主动消息靠
            # 这个字段躲开正在聊天的人；写在 record_silence 之前，因为
            # 沉默也是“人来过”的一种。
            record_attention(
                conn,
                persona.state_key,
                person_key,
                role=role,
                dynamics=dynamics,
                confidence=1.0,
                timezone_name=self.settings.quota_timezone,
            )
            if not decision.reply:
                record_silence(
                    conn,
                    persona.state_key,
                    person_key,
                    role=role,
                    dynamics=dynamics,
                    timezone_name=self.settings.quota_timezone,
                )
                # 累到说不出话时，把这条话留成待续话题：以后的主动消息
                # 才有具体的东西可说。这段失败不能改变“这轮沉默”的结果。
                try:
                    pending_topic.maybe_create_pending_topic(
                        conn,
                        persona_key=persona.state_key,
                        person_key=person_key,
                        topic=content,
                        state=signals.state,
                        timezone_name=self.settings.quota_timezone,
                    )
                except Exception:  # noqa: BLE001 - 待续话题只是增强
                    logger.warning("记录待续话题失败 person=%s", person_key, exc_info=True)

            delta = None
            if call is not None:
                delta = self._apply_appraisal_safe(
                    conn,
                    incoming,
                    person_key=person_key,
                    role=role,
                    persona=persona,
                    call=call,
                )
            # 数值改动下一轮才生效，但语气要立刻跟上：这一句里被伤害了，
            # 回复就不能还像没事人。其它慢变量仍用掷骰子前的快照。
            prompt_state = signals.state
            if delta is not None and delta.stage_after:
                prompt_state = replace(
                    signals.state, conflict_stage=delta.stage_after
                )
            return decision, prompt_state
        except Exception:  # noqa: BLE001 - 决策失败不能让私聊变哑
            logger.warning(
                "QQ 私聊决策失败，回退为直接回复 qq=%s",
                incoming.user_id,
                exc_info=True,
            )
            return None, None
        finally:
            if conn is not None:
                conn.close()

    def _apply_appraisal_safe(
        self,
        conn: Any,
        incoming: Incoming,
        *,
        person_key: str,
        role: str,
        persona: PersonaVersion,
        call: AppraisalCall,
    ) -> Any:
        """评估映射与落账；失败只记日志，不影响已经做出的决策。

        返回值是映射后的增量（含落账后的冲突阶段）；失败时返回 None。
        """
        try:
            if call.status != "ok" or call.result is None:
                record_appraisal_log(
                    conn,
                    persona_key=persona.state_key,
                    person_key=person_key,
                    message_id=(
                        None
                        if incoming.message_id is None
                        else str(incoming.message_id)
                    ),
                    result=None,
                    status=call.status,
                    raw_output=call.raw_output,
                    latency_ms=call.latency_ms,
                )
                return None

            delta = apply_appraisal(
                conn,
                persona.state_key,
                person_key,
                role=role,
                dynamics=persona.dynamics,
                result=call.result,
                timezone_name=self.settings.quota_timezone,
            )
            record_appraisal_log(
                conn,
                persona_key=persona.state_key,
                person_key=person_key,
                message_id=(
                    None
                    if incoming.message_id is None
                    else str(incoming.message_id)
                ),
                result=call.result,
                delta=delta,
                status="ok",
                raw_output=call.raw_output,
                latency_ms=call.latency_ms,
            )
            return delta
        except Exception:  # noqa: BLE001
            logger.warning(
                "应用关系评估失败 qq=%s", incoming.user_id, exc_info=True
            )
            return None

    async def _run_chat_turn(
        self,
        incoming: Incoming,
        content: str,
        *,
        binding: object | None,
        is_admin: bool,
        is_private: bool,
        relationship: tuple[str, str, str] | None = None,
        dynamics: Any = None,
        state_context: str | None = None,
        baseline_affection: bool = True,
    ) -> dict[str, Any]:
        """额度 → 记忆 → agent → 入库 → 发送；群聊和私聊共用。

        `baseline_affection=False` 表示本轮已经由模型评估落过关系账，
        回复本身不再叠加通用的“聊天好感/心情”加成，避免双重计数。
        """
        key = (incoming.group_id, incoming.user_id)

        # 额度：管理员不计数；用尽时回固定文案，不调模型。
        status, usage = self._reserve(
            qq_uin=str(incoming.user_id),
            user_id=getattr(binding, "user_id", None),
            tier=getattr(binding, "tier", "user"),
            is_admin=is_admin,
        )
        if status == "user_exhausted":
            return await self._send(
                incoming, commands.QUOTA_USER, reason="quota_exhausted"
            )
        if status == "global_exhausted":
            return await self._send(
                incoming, commands.QUOTA_GLOBAL, reason="quota_exhausted"
            )

        try:
            # 取这一轮要注入的记忆。失败时为空字符串，回复照常。
            try:
                memory_context = await asyncio.to_thread(
                    pipeline.qq_memory_context,
                    incoming.user_id,
                    incoming.group_id or None,
                    binding,
                    content,
                )
            except Exception:  # noqa: BLE001 - 记忆只是增强
                logger.warning("QQ 记忆检索失败 qq=%s", incoming.user_id, exc_info=True)
                memory_context = ""

            reply, messages_for_history, produced, ok = await self._reply(
                key,
                content,
                is_admin=is_admin,
                channel="qq_private" if is_private else "qq_group",
                memory_context=memory_context or None,
                state_context=state_context,
            )
        except asyncio.CancelledError:
            # 被新消息打断的回合什么都没发出去（半截回复只留在上下文里），
            # 额度要退回；否则用户多说两句就会白白烧掉当天的限额。
            self._refund(usage)
            raise

        normal_reply = produced and ok
        if produced:
            # 已经输出过正文就说明用户看到了内容，不退款；但报错（可能是
            # 半截之后报错）的回合不能入库，否则残句会被当成完整回答提炼。
            if ok:
                if messages_for_history:
                    self._history[key] = messages_for_history
                # QQ 侧在回复产出后一次性写两条事件：这一轮整体入库。
                turn = await asyncio.to_thread(
                    pipeline.record_qq_turn,
                    incoming.user_id,
                    incoming.group_id or None,
                    binding,
                    content,
                    reply,
                )
                if turn is not None:
                    pipeline.schedule_extraction(turn)
                if relationship is not None and dynamics is not None:
                    await asyncio.to_thread(
                        self._record_reply_safe,
                        relationship,
                        dynamics,
                        self.settings.quota_timezone,
                        baseline_affection,
                    )
        else:
            # 没产出正文（模型报错/断流）：这次不算，退款，也别污染上下文。
            self._refund(usage)
            reply = commands.AGENT_ERROR

        return await self._send(
            incoming, reply, reason=None if normal_reply else "agent_error"
        )

    @staticmethod
    def _record_reply_safe(
        relationship: tuple[str, str, str],
        dynamics: Any,
        timezone_name: str,
        baseline: bool = True,
    ) -> None:
        """回复成功后的关系变化；失败只记日志，不影响已经发出的回复。"""
        persona_key, person_key, role = relationship
        conn = None
        try:
            conn = memory_db.connect()
            memory_db.init_schema(conn)
            record_reply(
                conn,
                persona_key,
                person_key,
                role=role,
                dynamics=dynamics,
                timezone_name=timezone_name,
                baseline=baseline,
            )
        except Exception:  # noqa: BLE001
            logger.warning("记录 QQ 关系事件失败 person=%s", person_key, exc_info=True)
        finally:
            if conn is not None:
                conn.close()

    def _remember_interrupted(
        self, key: tuple[int, int], content: str, partial: str
    ) -> None:
        """被打断时把半截回复写进历史；没有正文就不写。"""
        if not partial:
            return
        history = self._history.get(key, [])
        new_history = [
            *history,
            ChatMessage(role="user", content=content[:8000]),
            ChatMessage(role="assistant", content=partial[:8000]),
        ]
        max_messages = max(0, self.settings.qq_history_turns) * 2
        self._history[key] = new_history[-max_messages:] if max_messages else []

    async def _send(
        self, incoming: Incoming, reply: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """按来源把回复发回群/私聊；发送失败只记日志。

        私聊的正常回复会切成多条气泡并加打字间隔；命令、引导、额度、
        错误这些固定文案不延迟——用户等的是一个确定的结果，不是表演。
        """
        result: dict[str, Any] = {"handled": True, "reply": reply}
        if reason:
            result["reason"] = reason

        paced = incoming.message_type == "private" and reason is None
        dynamics = self._persona().dynamics if paced else None
        if paced:
            chunks = segment_bubbles(reply, dynamics, rng=self._rng)
        else:
            chunks = []
        if not chunks:
            chunks = self._split_text(reply)

        delayed = 0.0
        try:
            for index, chunk in enumerate(chunks):
                if index and dynamics is not None:
                    delay = bubble_delay(chunks[index - 1], dynamics, rng=self._rng)
                    # 整轮最多等 5.5 秒；超出的气泡仍然发送，只是不再等。
                    if delayed + delay <= 5.5:
                        delayed += delay
                        await asyncio.sleep(delay)
                if incoming.message_type == "private":
                    await self.sender.send_private_message(incoming.user_id, chunk)
                else:
                    await self.sender.send_group_message(incoming.group_id, chunk)
        except OneBotError as exc:
            logger.warning(
                "QQ 回复发送失败（%s %s）：%s",
                incoming.message_type,
                incoming.group_id or incoming.user_id,
                exc,
            )
            result["send_error"] = str(exc)
        except Exception:  # noqa: BLE001 - 后台任务不能把异常抛给进程
            logger.exception("QQ 回复发送时出现未预期错误")
            result["send_error"] = "unexpected_error"

        return result

    # ------------------------------------------------------------------
    # 额度
    # ------------------------------------------------------------------
    def _quota_gate(self) -> QQQuotaGate:
        """惰性取额度门面；导入时就建库会让 --help 之类也写文件。"""
        if self.gate is None:
            self.gate = get_qq_quota_gate()
        return self.gate

    def _reserve(
        self, *, qq_uin: str, user_id: int | None, tier: str, is_admin: bool
    ) -> tuple[str, QQUsage | None]:
        """占一次额度；返回 (状态, 可用于退款的凭据)。

        额度库出错时放行而不是拦人：配额是防滥用，不是可用性的前提。
        """
        try:
            return self._quota_gate().reserve(
                qq_uin=qq_uin, user_id=user_id, tier=tier, is_admin=is_admin
            )
        except Exception:  # noqa: BLE001 - 额度故障不该让聊天整体不可用
            logger.exception("QQ 额度预占失败，本次放行")
            return "ok", None

    def _refund(self, usage: QQUsage | None) -> None:
        """没产出正文就退回这次占用的额度。"""
        if usage is None:
            return
        try:
            self._quota_gate().refund(usage)
        except Exception:  # noqa: BLE001 - 退款失败只影响计数，不影响回复
            logger.exception("QQ 额度退款失败")

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def _parse_message(self, payload: dict[str, Any]) -> Incoming | None:
        """从 OneBot 事件里取出群聊/私聊文本；其他事件返回 None。

        OneBot 11 的群消息大致长这样：
        ```json
        {
          "post_type": "message",
          "message_type": "group",
          "self_id": 123456,
          "group_id": 789,
          "user_id": 456,
          "message_id": 1001,
          "message": [
            {"type": "at", "data": {"qq": "123456"}},
            {"type": "text", "data": {"text": " 你好"}}
          ]
        }
        ```
        私聊把 message_type 换成 "private"，没有 group_id。
        不同实现的 `message` 可能是字符串（CQ 码），这里只处理数组形式；
        字符串形式先忽略，避免在没有验证过的格式上做错误解析。
        """
        if payload.get("post_type") != "message":
            return None
        message_type = payload.get("message_type")
        if message_type not in {"group", "private"}:
            return None

        try:
            user_id = int(payload.get("user_id") or 0)
            self_id = int(payload.get("self_id") or 0)
            group_id = int(payload.get("group_id") or 0) if message_type == "group" else 0
        except (TypeError, ValueError):
            return None
        if not user_id or (message_type == "group" and not group_id):
            return None

        message = payload.get("message")
        if not isinstance(message, list):
            return None

        parts: list[str] = []
        at_self = False
        for segment in message:
            if not isinstance(segment, dict):
                continue
            segment_type = segment.get("type")
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            if segment_type == "text":
                parts.append(str(data.get("text") or ""))
            elif segment_type == "at" and self_id:
                if str(data.get("qq") or "") == str(self_id):
                    at_self = True

        message_id_raw = payload.get("message_id")
        try:
            message_id = int(message_id_raw) if message_id_raw is not None else None
        except (TypeError, ValueError):
            message_id = None

        return Incoming(
            message_type=message_type,
            group_id=group_id,
            user_id=user_id,
            self_id=self_id,
            text="".join(parts).strip(),
            at_self=at_self,
            message_id=message_id,
        )

    def _match_trigger(
        self,
        text: str,
        at_self: bool,
        *,
        trigger: str | None = None,
    ) -> TriggerMatch:
        """按群策略判断是否触发，并去掉触发前缀。"""
        return match_trigger(
            text,
            at_self,
            trigger=trigger or self._trigger,
            prefix=self.settings.qq_prefix,
        )

    def _group_policy(self, group_id: int) -> GroupPolicy:
        """读群策略；任何异常都回退到 `QQ_TRIGGER` 推导的默认值。

        策略库读不出来时宁可沿用旧行为，也不要让整个群突然不回话。
        """
        fallback = default_policy(self._trigger)
        try:
            conn = memory_db.connect()
            try:
                memory_db.init_schema(conn)
                return load_policy(conn, group_id, fallback=fallback)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - 策略库不可用不该影响聊天
            logger.warning("读取群策略失败 group=%s，回退到默认策略", group_id, exc_info=True)
            return fallback

    async def _allowed_group_ids(self) -> set[int]:
        """群白名单以运行时表为准；环境变量只在首次播种时起作用。

        读不出来时返回空集合（哪个群都不去）：默认拒绝比误回陌生群安全。
        """
        try:
            return await asyncio.to_thread(self._read_allowed_group_ids)
        except Exception:  # noqa: BLE001 - 白名单库故障不该让进程崩
            logger.warning("读取群白名单失败，本轮不处理群消息", exc_info=True)
            return set()

    def _read_allowed_group_ids(self) -> set[int]:
        conn = memory_db.connect()
        try:
            memory_db.init_schema(conn)
            return group_allowlist.allowed_group_ids(
                conn, env_groups=self.settings.qq_allowed_group_ids
            )
        finally:
            conn.close()

    def _is_duplicate(self, group_id: int, message_id: int | None) -> bool:
        """记录并判断 (群号, message_id) 是否已经处理过。"""
        if message_id is None:
            return False
        key = (group_id, message_id)
        if key in self._seen:
            return True
        self._seen[key] = asyncio.get_running_loop().time()
        while len(self._seen) > MAX_SEEN_MESSAGES:
            self._seen.popitem(last=False)
        return False

    # ------------------------------------------------------------------
    # 调用 agent
    # ------------------------------------------------------------------
    async def _reply(
        self,
        key: tuple[int, int],
        content: str,
        *,
        is_admin: bool,
        channel: str,
        memory_context: str | None = None,
        state_context: str | None = None,
    ) -> tuple[str, list[ChatMessage], bool, bool]:
        """调用 agent，返回 (要发送的文本, 更新后的历史, 是否产出过正文, 是否成功)。

        第三个返回值给额度层用：没产出正文的失败要退款。
        第四个返回值给记忆层用：报错的回合不写事件、不进历史——哪怕已经
        输出了半截正文（用户看到了内容，所以不退款，但残句不能入库）。

        `channel` 只影响人设覆盖层。QQ 里她是在用通讯法阵和人说话，
        眼前没有书架、没有页面，所以 page_url/page_title 一律传 None。
        """
        history = self._history.get(key, [])
        messages = [*history, ChatMessage(role="user", content=content[:8000])]

        parts: list[str] = []
        error_message = ""
        try:
            async for event in self._stream(
                messages,
                page_url=None,
                page_title=None,
                is_admin=is_admin,
                channel=channel,
                memory_context=memory_context,
                state_context=state_context,
            ):
                event_type = event.get("type")
                if event_type == "delta":
                    parts.append(str(event.get("content") or ""))
                elif event_type == "error":
                    error_message = str(event.get("message") or "AI 服务暂时不可用。")
        except asyncio.CancelledError:
            # 生成中被新消息打断：已经产出的半截正文留在上下文里，
            # 下一轮模型知道“刚才那句话没说完”。
            self._remember_interrupted(key, content, "".join(parts).strip())
            raise
        except Exception:  # noqa: BLE001 - agent 内部已经会捕获大部分异常
            logger.exception("QQ 通道调用 agent 失败")
            error_message = "AI 服务暂时不可用，请稍后再试。"

        reply = "".join(parts).strip()
        if not reply:
            reply = error_message or "我在，但这次没想好怎么回。"

        # 与网页 chat.py 保持一致：只有真正产出非空正文才算“答上了”。
        # 空 delta 也算没交付，额度要退、历史不能写。
        produced = any(part.strip() for part in parts)

        # agent 出错时不把错误（哪怕是半截回复）写进历史/时间线，
        # 避免下一轮模型把失败内容当成事实。
        if error_message:
            return reply, history, produced, False

        # 只保留最近 N 轮。1 轮 = 用户一条 + 助手一条。
        new_history = [
            *messages,
            ChatMessage(role="assistant", content=reply[:8000]),
        ]
        max_messages = max(0, self.settings.qq_history_turns) * 2
        if max_messages:
            new_history = new_history[-max_messages:]
        else:
            new_history = []
        return reply, new_history, produced, True

    # ------------------------------------------------------------------
    # 输出格式
    # ------------------------------------------------------------------
    @staticmethod
    def _split_text(text: str) -> list[str]:
        """把长回复拆成适合群聊的短消息。

        先按换行切，再硬切超长段落。不要按标点做复杂分段：
        QQ 的显示宽度和字体不可控，简单可靠比“漂亮”重要。
        """
        text = re.sub(r"\x00", "", text).strip()
        if not text:
            return ["我在，但这次没想好怎么回。"]

        chunks: list[str] = []
        current = ""
        for line in text.splitlines():
            line = line.rstrip()
            if not line:
                if current:
                    current += "\n"
                continue
            while len(line) > MAX_MESSAGE_CHARS:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(line[:MAX_MESSAGE_CHARS])
                line = line[MAX_MESSAGE_CHARS:]
            if not current:
                current = line
            elif len(current) + 1 + len(line) <= MAX_MESSAGE_CHARS:
                current += "\n" + line
            else:
                chunks.append(current)
                current = line

        if current:
            chunks.append(current)
        return chunks or [text[:MAX_MESSAGE_CHARS]]
