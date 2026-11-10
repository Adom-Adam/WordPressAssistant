"""对话接口：认证 → 额度 → SSE 流。

SSE（Server-Sent Events）是一种“服务器持续往浏览器推文本”的 HTTP 协议。
它的报文格式非常简单，每个事件就是几行文本，以空行结束：

    data: {"type": "delta", "content": "你"}

    data: {"type": "delta", "content": "好"}

    data: {"type": "done"}

前端用 fetch + ReadableStream 读取，按空行切分，再 JSON.parse。

为什么不用 WebSocket：
- 对话是单向流（服务器 → 浏览器），SSE 足够；
- SSE 走普通 HTTP/HTTPS，Nginx 反向代理配置简单；
- 浏览器断线重连、代理穿透都比 WebSocket 省事。

`EventSourceResponse` 来自 sse-starlette，它替我们处理了：
- 正确的 Content-Type: text/event-stream；
- 客户端断开时取消生成器（避免协程泄漏）；
- `ping=15`：每 15 秒发一个注释行作为心跳，防止 Nginx/负载均衡
  把长时间没有数据的连接当成“僵死连接”掐掉。

第二步新增的三件事（顺序很重要）：

    1. 认证：没有有效 JWT 的请求在进入模型之前就被拒绝（401）；
    2. 额度：普通用户先原子占位，占不到就返回 429；
    3. 退款：如果模型一个字都没输出就失败/超时/断开，把额度还回去。

认证和额度都必须在**流开始之前**完成。SSE 一旦开始发送响应头，
就没法再把 HTTP 状态码改成 401/429 了。

第五步加了 `/chat/anonymous`：没有登录的人也能说话，代价是她不会记住
你。匿名请求同样要先占额度（按 IP 哈希 + 全站上限），但整条链路里
**不写任何内容**——不记事件、不检索记忆、不提炼。
"""

import asyncio
import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.agent.core import stream_reply
from app.anonymous_quota import (
    AnonymousQuotaStore,
    AnonymousReservation,
    get_anonymous_quota_store,
)
from app.api.deps import require_user
from app.auth import AuthUser
from app.channels.qq.copy import QQCopy
from app.config import get_settings
from app.credits import get_credit_store
from app.memory import pipeline
from app.quota import GlobalQuotaExceeded, QuotaReservation, QuotaStore, get_quota_store
from app.runtime_settings import get_runtime_store
from app.schemas import ChatRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def client_ip(request: Request) -> str:
    """真实访客 IP。

    Nginx 在我们前面，`request.client.host` 永远是代理地址，所以优先读
    `X-Forwarded-For`。取**最后一个**而不是第一个：第一个是客户端自己
    写的，可以随便伪造；最后一个是最近一跳代理追加的，更可信。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def anonymous_scope(ip: str) -> str:
    """把 IP 变成不可逆的计数键。

    数据库里只留哈希：攒一份“谁在什么时候来过”的原始日志没有意义，
    还会给自己添一份需要保护的数据。加盐是为了防止有人拿彩虹表反查。
    """
    salt = get_settings().l2d_ai_jwt_secret.strip() or "l2d-ai"
    digest = hashlib.sha256(f"{ip}:{salt}".encode()).hexdigest()[:32]
    return f"web:{digest}"


@router.post("/chat")
async def chat(
    request: ChatRequest,
    request_obj: Request,
    user: AuthUser = Depends(require_user),
) -> EventSourceResponse:
    """登录用户的对话入口。认证交给 `require_user`，其余和匿名共用。"""
    return await _chat_response(
        request, user=user, anonymous=False, request_obj=request_obj
    )


@router.post("/chat/anonymous")
async def chat_anonymous(
    request: ChatRequest,
    request_obj: Request,
) -> EventSourceResponse:
    """匿名对话入口：不需要 token，也不记任何内容。

    关了开关就 403——这里必须显式拒绝，不能靠“没有 token 所以走别的分
    支”，否则前端会看到一个和登录接口一样的 401，误以为要去重新登录。
    """
    return await _chat_response(
        request, user=None, anonymous=True, request_obj=request_obj
    )


async def _chat_response(
    request: ChatRequest,
    *,
    user: AuthUser | None,
    anonymous: bool,
    request_obj: Request,
) -> EventSourceResponse:
    """两个入口的公共部分：占额度 → 建流 → 返回响应头。

    这个函数本身**不会等待 AI 回复生成完**，它只做四件事：
    1. 检查并占用额度；
    2. 创建一个异步生成器 `event_stream`；
    3. 立刻返回 `EventSourceResponse`（响应头先发给浏览器）；
    4. 生成器结束时，按需退款。
    """
    # 额度、调用上限、模型名都以“运行时设置”为准：
    # 管理员在 WP 后台改过就立即生效，没改过则等于 .env 默认值。
    runtime = get_runtime_store().load()

    reservation: QuotaReservation | AnonymousReservation
    web_store: QuotaStore | None = None
    anon_store: AnonymousQuotaStore | None = None
    tier = "guest"

    if anonymous:
        # ------------------------------------------------------------------
        # 匿名额度。
        #
        # 和登录用户完全分开记账：一群游客不该把会员当天的份吃光。
        # 个人上限为 0 表示这条通道关掉了；全站上限为 0 表示不限量，
        # 两个 0 的含义不一样（见 anonymous_quota.py）。
        # ------------------------------------------------------------------
        if not runtime.anonymous_web_enabled:
            raise HTTPException(
                status_code=403,
                detail="匿名聊天现在没有开放，去图书馆登记一下就能聊了～",
            )

        scope = anonymous_scope(client_ip(request_obj))
        anon_store = get_anonymous_quota_store()
        try:
            result = await asyncio.to_thread(
                anon_store.reserve,
                scope,
                runtime.anonymous_web_daily_quota,
                global_limit=runtime.anonymous_global_daily_quota,
            )
        except Exception:  # noqa: BLE001 - 数据库故障时“宁可不服务，也不放开”
            logger.exception("匿名额度服务异常 scope=%s", scope)
            raise HTTPException(
                status_code=503,
                detail="额度服务暂时不可用，请稍后再试",
            ) from None

        if result.free is None:
            # 文案走人设：她不会对访客说“额度”。
            copy = QQCopy.from_runtime(runtime, place=get_settings().wp_site_url)
            detail = (
                copy.anon_quota_global
                if result.status == "global_exhausted"
                else copy.anon_quota_user
            )
            raise HTTPException(
                status_code=429,
                detail=detail,
                headers={"Retry-After": str(anon_store.seconds_until_reset())},
            )
        reservation = result.free

    else:
        # ------------------------------------------------------------------
        # 登录用户的额度。
        #
        # 为什么用 asyncio.to_thread：
        # sqlite3 的调用是同步阻塞的。如果数据库恰好被另一个请求锁住，
        # 直接调用会让整个事件循环停住，所有用户一起等。
        # to_thread 把这次调用丢到线程池，事件循环可以继续处理别的请求。
        #
        # 为什么先占位再调模型：
        # 如果等回答完再扣次数，用户同时发 10 个请求就能突破上限。
        # reserve() 内部用一条原子 SQL 完成“判断 + 扣减”，并发安全。
        # ------------------------------------------------------------------
        assert user is not None  # 认证依赖已经保证；这里只是给类型检查看
        web_store = get_quota_store()
        try:
            # 会员和普通用户用不同的每日上限。等级存在后端库里，不在 JWT 里：
            # token 有 15 分钟有效期，管理员刚把某人升成会员，旧 token 还会
            # 带着旧的等级，按 token 判断会慢半拍。
            if user.is_admin:
                tier = "admin"
            else:
                tier, _expires_at = await asyncio.to_thread(
                    get_credit_store().tier_of, user.id
                )
            limit = (
                runtime.member_daily_quota
                if tier == "member"
                else runtime.daily_quota
            )
            web_reservation = await asyncio.to_thread(
                web_store.reserve,
                user.id,
                limit,
                is_admin=user.is_admin,
                global_limit=runtime.global_daily_quota,
            )
        except GlobalQuotaExceeded:
            # 全站总电闸跳了。它和“个人额度用完”是两回事，
            # 文案要分开，否则用户会以为是自己用太多了。
            retry_after = web_store.seconds_until_reset()
            logger.info(
                "全站额度用尽 user=%s day=%s limit=%s",
                user.id,
                web_store.today(),
                runtime.global_daily_quota,
            )
            raise HTTPException(
                status_code=429,
                detail="今天全站的聊天额度已经用完了，明天再来吧～",
                headers={"Retry-After": str(retry_after)},
            ) from None
        except Exception:  # noqa: BLE001 - 数据库故障时“宁可不服务，也不放开”
            logger.exception("额度服务异常 user=%s", user.id)
            raise HTTPException(
                status_code=503,
                detail="额度服务暂时不可用，请稍后再试",
            ) from None

        if web_reservation is None:
            # 普通用户额度用完。429 Too Many Requests 是最贴切的状态码。
            # Retry-After 告诉客户端“多少秒后再来”，避免它立刻重试。
            retry_after = web_store.seconds_until_reset()
            logger.info(
                "额度用尽 user=%s day=%s limit=%s",
                user.id,
                web_store.today(),
                limit,
            )
            raise HTTPException(
                status_code=429,
                detail=f"今天的聊天额度（{limit} 条）已经用完啦，明天再来～",
                headers={"Retry-After": str(retry_after)},
            )

        reservation = web_reservation

    # ------------------------------------------------------------------
    # 定义 SSE 生成器。
    #
    # 为什么要在端点里再定义一个 async 函数？
    #
    # 1. `async def` + `yield` = **异步生成器函数**。
    #    注意：调用 `event_stream()` 时，函数体并不会立刻执行，
    #    只会创建一个“异步生成器对象”；真正开始执行，是在
    #    `EventSourceResponse` 迭代它的时候。这正是“惰性生产”的关键。
    #
    # 2. 不能把 `yield` 直接写在路径函数里：
    #    路径函数一旦包含 `yield`，它自己就变成了异步生成器函数，
    #    而 FastAPI 要求路径函数返回 Response（或可序列化数据），
    #    不会自动把生成器包装成流式响应。所以必须分成两层：
    #    “外层立刻返回 Response，内层按需产出数据”。
    #
    # 3. 为什么用内层函数（闭包）：
    #    可以直接读取外层的 request、reservation，不必再把参数传一遍。
    # ------------------------------------------------------------------
    async def event_stream():
        # produced 记录“是否已经给用户输出过正文”。
        # 退款规则只看它：
        # - 一个字都没输出就失败 → 退；
        # - 已经说过话 → 不退（模型确实花了 token，用户也看到了内容）。
        produced = False

        # 这一轮的用户原话：既用于检索记忆，也作为时间线里用户事件的正文。
        query = next(
            (
                item.content
                for item in reversed(request.messages)
                if item.role == "user"
            ),
            "",
        )
        user_event_id: int | None = None
        memory_context = ""
        parts: list[str] = []
        errored = False

        try:
            # 匿名回合什么都不记：没有 user_event_id 就没有“一问一答”
            # 这个概念，也就不会触发提炼。
            #
            # 用户事件在流开始前写入：哪怕模型随后失败，用户说过的话也已经
            # 进入时间线。两个调用都是同步 SQLite，用 to_thread 避免卡住事件循环。
            #
            # 放在 try 里面而不是前面：写事件/检索记忆本身出错（或客户端
            # 恰好在这时断开）时，finally 的退款仍然会执行，不会白扣额度。
            if not anonymous:
                assert user is not None
                user_event_id = await asyncio.to_thread(
                    pipeline.record_web_user_event, user.id, query
                )
                memory_context = await asyncio.to_thread(
                    pipeline.web_memory_context, user.id, query
                )
            # asyncio.timeout 是 Python 3.11 的写法：
            # 代码块超过 90 秒会被取消，并在这里抛出 TimeoutError。
            # 它防的是“工具卡死”这类调用次数不会增加的问题。
            async with asyncio.timeout(runtime.agent_max_seconds):
                # 这里只做“内部事件 → SSE 数据帧”的转换：
                # - agent 层产出 {"type": "delta", ...} 这种业务事件；
                # - API 层负责把它序列化成传输格式。
                # 两层解耦后，以后换传输方式（WebSocket 等）不用改 AI 代码。
                #
                # ensure_ascii=False 让中文以 UTF-8 原样输出，
                # 否则会变成 你好 这类转义（能工作，但调试时难读）。
                async for event in stream_reply(
                    messages=request.messages,
                    page_url=request.page_url,
                    page_title=request.page_title,
                    is_admin=bool(user and user.is_admin),
                    memory_context=memory_context or None,
                ):
                    if event.get("type") == "delta" and event.get("content"):
                        produced = True
                        parts.append(str(event.get("content") or ""))
                    elif event.get("type") == "error":
                        # agent 内部报错是作为事件发出来的，不会抛异常；
                        # 如果之前已经输出过半截正文，这里仍要记下来，否则
                        # 循环正常结束后会把半截回复当成完整回答入库。
                        errored = True
                    yield {"data": json.dumps(event, ensure_ascii=False)}

                # 只有流正常走完且没有报错才写助手事件：超时、异常、
                # 客户端中断、半截报错都不写，避免把失败的回复当成
                # 完整的一问一答去提炼。
                if (
                    not anonymous
                    and produced
                    and not errored
                    and "".join(parts).strip()
                ):
                    assert user is not None
                    turn = await asyncio.to_thread(
                        pipeline.record_web_assistant_event,
                        user.id,
                        "".join(parts),
                        user_event_id,
                    )
                    if turn is not None:
                        pipeline.schedule_extraction(turn)

        except TimeoutError:
            # 超时不一定代表出错：可能是模型/工具卡住了。
            # 给用户一句可行动的提示，而不是让他对着转圈等。
            logger.warning(
                "对话超时 user=%s seconds=%s",
                user.id if user is not None else "anonymous",
                runtime.agent_max_seconds,
            )
            yield {
                "data": json.dumps(
                    {
                        "type": "error",
                        "message": "这次处理时间有点久，我先停下来了，请再问我一次～",
                    },
                    ensure_ascii=False,
                )
            }

        except asyncio.CancelledError:
            # 客户端断开连接（关闭页面/取消请求）时，FastAPI 会取消这个协程。
            # 必须原样抛出，让框架完成清理，不要吞掉。
            # 退款统一放在下面的 finally 里处理。
            raise

        except Exception:  # noqa: BLE001 - 兜底，避免异常泄漏到 SSE 框架
            logger.exception(
                "对话流异常 user=%s", user.id if user is not None else "anonymous"
            )
            yield {
                "data": json.dumps(
                    {
                        "type": "error",
                        "message": "AI 服务暂时不可用，请稍后再试。",
                    },
                    ensure_ascii=False,
                )
            }

        finally:
            # finally 在任何退出路径都会执行：正常结束、超时、异常、客户端断开。
            # 这里用同步调用而不是 await：
            # 协程被取消时，await 可能立刻再次抛出 CancelledError，
            # 导致退款没执行完。退一次额度只是一条本地 SQL，耗时可以忽略。
            if not produced:
                try:
                    if anonymous:
                        assert anon_store is not None
                        anon_store.refund(reservation)
                    else:
                        assert web_store is not None
                        web_store.refund(reservation)
                except Exception:  # noqa: BLE001 - 退款失败不能破坏已发出的 SSE
                    # 最坏情况只是少退一次额度；把错误留在服务端日志，
                    # 不要让它覆盖超时/断开的原始错误。
                    logger.exception(
                        "退还额度失败 user=%s",
                        user.id if user is not None else "anonymous",
                    )

    # ------------------------------------------------------------------
    # 返回 SSE 响应。
    #
    # ping=15：每 15 秒发送一个 SSE 注释行作为心跳，
    # 防止 Nginx/负载均衡把长时间没有数据的连接断开。
    #
    # 额度响应头在流开始前就固定了，所以前端读完头再消费正文即可，
    # 不需要为了刷新额度额外发一次请求。
    # ------------------------------------------------------------------
    headers: dict[str, str] = {}
    if anonymous:
        assert isinstance(reservation, AnonymousReservation)
        headers["X-Anonymous-Remaining"] = str(reservation.remaining)
        headers["X-Anonymous-Limit"] = str(reservation.limit)
        if reservation.global_remaining is not None:
            headers["X-Anonymous-Global-Remaining"] = str(
                reservation.global_remaining
            )
            headers["X-Anonymous-Global-Limit"] = str(reservation.global_limit)
        return EventSourceResponse(event_stream(), ping=15, headers=headers)

    assert isinstance(reservation, QuotaReservation)
    if reservation.remaining is not None:
        headers["X-Quota-Remaining"] = str(reservation.remaining)
        headers["X-Quota-Limit"] = str(reservation.limit)
    elif not reservation.counted:
        # 管理员的剩余次数是“无限”，用一个标记表示，避免前端误解析成 0。
        headers["X-Quota-Unlimited"] = "1"

    # 全站剩余额度只在启用时返回；管理员不参与全站计数，所以没有这个头。
    if reservation.global_remaining is not None:
        headers["X-Global-Quota-Remaining"] = str(reservation.global_remaining)
        headers["X-Global-Quota-Limit"] = str(reservation.global_limit)

    # 额外额度余额和身份：前端额度条要显示“今日 3/20 ・ 存量 5”，
    # 兑换码入口也要根据身份决定写什么文案。
    headers["X-Quota-Credit"] = str(reservation.credit_balance or 0)
    headers["X-Quota-Tier"] = tier

    return EventSourceResponse(event_stream(), ping=15, headers=headers)
