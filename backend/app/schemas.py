"""请求/响应的数据模型。

Pydantic 的 BaseModel 会在请求进入业务代码之前完成校验和类型转换：
- 字段缺失、类型不对，FastAPI 会自动返回 422 和清晰的错误信息；
- `Field(min_length=...)` / `max_length=...` 用来限制长度，防止超大请求拖垮模型调用；
- `Literal[...]` 限定枚举值，客户端不能伪造 `system` / `tool` 角色，
  这也是一种提示词注入防护。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """对话中的一条消息。"""

    # 只允许前端传 user / assistant 两种角色。
    # system 由后端根据人设和页面上下文生成；tool 由 LangChain 内部管理。
    role: Literal["user", "assistant"]

    # 单条消息最多 8000 字符。max_length 在 Pydantic v2 中作用于字符串长度。
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    """POST /api/chat 的请求体。"""

    # 一次最多 40 条，后端还会按 agent_max_history 再截取最近 N 条。
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)

    # 当前页面信息：让助手知道用户正在看哪篇文章。
    # 不传也完全没问题，属于可选的增强上下文。
    page_url: str | None = Field(default=None, max_length=2048)
    page_title: str | None = Field(default=None, max_length=512)


class RuntimeSettingsUpdate(BaseModel):
    """管理员可修改的运营参数。

    所有字段都是可选的：前端只提交改过的项，PATCH 语义比 PUT 更友好。
    但这里仍然用 PUT——WordPress 表单一次提交整组值，全量更新更好理解。

    `extra="forbid"` 会拒绝拼错的字段名（例如 global_quota 少了 daily），
    避免“保存成功但没生效”的静默错误。
    """

    model_config = ConfigDict(extra="forbid")

    daily_quota: int | None = Field(default=None, ge=0, le=1000)
    member_daily_quota: int | None = Field(default=None, ge=0, le=1000)
    global_daily_quota: int | None = Field(default=None, ge=0, le=1_000_000)
    credit_max_per_user: int | None = Field(default=None, ge=0, le=1_000_000)
    qq_daily_quota: int | None = Field(default=None, ge=0, le=1000)
    qq_member_daily_quota: int | None = Field(default=None, ge=0, le=1000)
    qq_global_daily_quota: int | None = Field(default=None, ge=0, le=1_000_000)
    qq_quota_shared: bool | None = None
    qq_unlimited: bool | None = None
    appraisal_enabled: bool | None = None
    appraisal_timeout_seconds: float | None = Field(default=None, ge=0.2, le=5.0)
    appraisal_global_daily_budget: int | None = Field(
        default=None, ge=0, le=1_000_000
    )
    appraisal_per_user_daily_budget: int | None = Field(
        default=None, ge=0, le=1000
    )
    proactive_enabled: bool | None = None
    proactive_tick_minutes: int | None = Field(default=None, ge=5, le=120)
    proactive_think_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    proactive_speak_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    proactive_min_interval_minutes: int | None = Field(default=None, ge=0, le=10_080)
    proactive_per_user_daily_limit: int | None = Field(default=None, ge=0, le=100)
    proactive_global_daily_limit: int | None = Field(
        default=None, ge=0, le=100_000
    )
    proactive_group_interval_minutes: int | None = Field(
        default=None, ge=0, le=10_080
    )
    proactive_group_daily_limit: int | None = Field(default=None, ge=0, le=100)
    anonymous_web_enabled: bool | None = None
    anonymous_qq_enabled: bool | None = None
    anonymous_guide_enabled: bool | None = None
    anonymous_web_daily_quota: int | None = Field(default=None, ge=0, le=1000)
    anonymous_qq_daily_quota: int | None = Field(default=None, ge=0, le=1000)
    anonymous_global_daily_quota: int | None = Field(
        default=None, ge=0, le=1_000_000
    )
    copy_private_guide: str | None = Field(default=None, max_length=500)
    copy_group_guide: str | None = Field(default=None, max_length=500)
    copy_anon_first_hint: str | None = Field(default=None, max_length=500)
    copy_anon_memory_hint: str | None = Field(default=None, max_length=500)
    copy_anon_quota_user: str | None = Field(default=None, max_length=500)
    copy_anon_quota_global: str | None = Field(default=None, max_length=500)
    copy_anon_group_ask: str | None = Field(default=None, max_length=500)
    agent_max_model_calls: int | None = Field(default=None, ge=1, le=20)
    agent_max_tool_calls: int | None = Field(default=None, ge=1, le=20)
    agent_max_seconds: float | None = Field(default=None, ge=5, le=300)
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class UserPatch(BaseModel):
    """管理员调整单个用户：等级、会员天数、备注。

    三个字段都可选（PATCH 语义）：只提交要改的那一项。
    会员必须带天数——等级是“有期限的”，不给期限就没法自动降级。
    """

    model_config = ConfigDict(extra="forbid")

    tier: Literal["user", "member"] | None = None
    duration_days: int | None = Field(default=None, ge=1, le=3650)
    note: str | None = Field(default=None, max_length=200)


class QQGroupPolicyUpdate(BaseModel):
    """管理员覆盖单个群的策略。

    特意不给 `probabilistic`：群聊不走私聊的概率模型，全群概率回复
    不在路线图上。群能配置的只有“怎么算被叫到”和“叫到了回不回”。
    """

    model_config = ConfigDict(extra="forbid")

    trigger: Literal["mention", "keyword", "all"]
    reply_policy: Literal["always", "silent"]
    proactive: bool


class BindCodeUpdate(BaseModel):
    """WordPress 把用户看到的绑定码同步过来。

    这里只校验长度；具体格式（8 位数字、允许 - 或空格分隔）由
    `is_valid_bind_code` 统一归一化后判断，避免两处规则漂移。
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)


class CreditGrant(BaseModel):
    """管理员手动增减额外额度。delta 为负表示扣减。"""

    model_config = ConfigDict(extra="forbid")

    delta: int = Field(ge=-1_000_000, le=1_000_000)
    reason: str = Field(min_length=1, max_length=100)


class CodeCreate(BaseModel):
    """批量生成兑换码。

    `value` 的含义随 `kind` 变化：credit 是“条数”，member 是“天数”。
    这是设计上刻意保留的一个小不对称——用户在兑换页只需要看到一个数字。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["credit", "member"]
    value: int = Field(ge=1, le=100_000)
    # 上限和 CreditStore.create_codes 的校验保持一致：接口层先拒绝，
    # 用户看到的是 422 而不是“生成了 0 个码”这种半成功。
    count: int = Field(default=1, ge=1, le=50)
    max_uses: int = Field(default=1, ge=1, le=1000)
    expires_at: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=200)


class RedeemRequest(BaseModel):
    """兑换码请求体。

    长度取宽一点（4~64）：用户可能带空格、小写、漏掉连字符，
    格式的真正判定交给 `CreditStore.redeem` 里的 `_normalize_code`，
    这里只拦明显不是兑换码的输入（空串、超长垃圾）。
    """

    code: str = Field(min_length=4, max_length=64)


class Citation(BaseModel):
    """知识库引用的结构。

    这是 `citations` SSE 事件里每一项的形状：
    - title/url 用来渲染来源卡片、点击跳转；
    - excerpt 是命中位置的摘要；
    - image_url 是命中的图片地址，前端把它显示成缩略图；
    - kind 区分“正文命中”和“图片描述命中”。

    字段使用 snake_case，和 `search_site` 工具返回的字典保持一致，
    避免在 agent 层来回做无意义的命名转换。
    """

    title: str
    url: str
    post_id: int | None = None
    excerpt: str | None = None
    image_url: str | None = None
    kind: Literal["article", "image"] | None = None


# 关系角色。只能在这四个里选：后台加角色必须同时在人设里写上限，
# 否则 `role_config` 会静默回退 normal，后台看起来“改了没生效”。
RoleName = Literal["owner", "exception", "special", "normal"]


class RoleUpdate(BaseModel):
    """后台把某个 WordPress 账号设成某个关系角色。"""

    model_config = ConfigDict(extra="forbid")

    wp_user_id: int = Field(ge=1)
    role: RoleName


class QQGroupAdd(BaseModel):
    """把一个群加进运行时白名单。"""

    model_config = ConfigDict(extra="forbid")

    group_id: int = Field(gt=0)


class QQWhitelistAdd(BaseModel):
    """把一个未绑定的 QQ 号加进后台白名单。

    `qq_uin` 只收纯数字：白名单是按号匹配的，允许字母或空格只会让
    “明明加了却不生效”变成一场排查。
    """

    model_config = ConfigDict(extra="forbid")

    qq_uin: str = Field(pattern=r"^\d{5,12}$")
    role: RoleName = "special"
    note: str | None = Field(default=None, max_length=200)


class QQWhitelistUpdate(BaseModel):
    """改白名单里某个号的角色 / 备注；`note` 不传表示保持不变。"""

    model_config = ConfigDict(extra="forbid")

    role: RoleName
    note: str | None = Field(default=None, max_length=200)
