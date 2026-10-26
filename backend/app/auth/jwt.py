"""验证 WordPress 签发的 JWT。

====================================================================
JWT 是什么
====================================================================

JWT（JSON Web Token）是一串用点分成三段的文本：

    header.payload.signature

- header（头部）：说明用什么算法签名，例如 {"alg": "HS256", "typ": "JWT"}；
- payload（载荷）：真正要传递的信息，例如用户 ID、过期时间；
- signature（签名）：用共享密钥对前两段做的 HMAC 计算。

把三段分别做 Base64URL 解码，前两段就是普通 JSON，**谁都能看**。
JWT 不负责保密，只负责“防篡改”：没有密钥就改不出正确的签名。

本项目的流程：
1. 用户在 WordPress 登录；
2. 插件用 `wp-config.php` 里的 L2D_AI_JWT_SECRET 签发 token；
3. 浏览器把 token 放在 `Authorization: Bearer <token>` 里发给后端；
4. 后端用**同一个密钥**重新计算签名，一致才相信 payload；
5. 后端只取 `sub`（用户 ID）和 `admin`（是否管理员），不信任其他字段做权限判断。

====================================================================
为什么用 PyJWT
====================================================================

JWT 的坑很多：算法混淆（把 alg 改成 none）、Base64URL 补齐、时间比较、
audience/issuer 校验、时钟偏差……自己手写验签很容易漏掉边界情况。
PyJWT 是 Python 生态最常用的实现，`jwt.decode()` 一行就能完成全部校验：

- algorithms=["HS256"]：只接受这一种算法，直接堵死 alg=none 攻击；
- audience / issuer：检查 token 是不是签给本服务、由 WordPress 签发的；
- require=[...]：缺少任何一个必需 claim 都直接失败；
- leeway：允许服务器之间有几秒时钟偏差；
- 过期时间 exp 会自动校验。

注意：这里只做“验证”，不做“签发”。签发在 WordPress 插件里完成，
后端永远不产生 token，也就不会把密钥写进日志或响应。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt as pyjwt

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 正常的 token 只有几百字节。设置上限是为了防止有人发一个巨大的字符串，
# 让服务在 Base64 解码/JSON 解析上白白消耗 CPU 和内存。
MAX_TOKEN_LENGTH = 4096


class AuthError(Exception):
    """认证失败（默认对应 HTTP 401）。

    为什么不直接在验证函数里抛 HTTPException：
    认证模块不应该依赖 FastAPI。它只表达“这个 token 不行”，
    由 API 层决定把它翻译成 401、403 还是别的状态码。
    这样写单元测试时也不必启动 Web 服务。
    """

    status_code = 401
    # 浏览器看到 401 + WWW-Authenticate，就知道应该重新获取凭证。
    headers = {"WWW-Authenticate": "Bearer"}

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AuthUnavailable(AuthError):
    """服务端没配好密钥（HTTP 503）。

    这种情况不是用户的错，也不能“放行”——没配置密钥时如果允许匿名访问，
    等于把收费接口公开给所有人。正确做法是明确拒绝，并让运维去补配置。
    """

    status_code = 503
    headers: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class AuthUser:
    """通过验签后得到的用户身份。

    frozen=True：创建后不可修改，避免下游代码不小心改掉用户 ID。
    slots=True：只允许这几个属性，减少内存占用，也能防止拼错字段名。
    """

    id: int
    name: str
    is_admin: bool


def verify_token(token: str, settings: Settings | None = None) -> AuthUser:
    """校验 token 并返回用户身份。

    参数：
    - token: 不含 "Bearer " 前缀的原始 JWT；
    - settings: 便于测试时注入配置；不传就读取全局 Settings。

    失败时抛出 AuthError / AuthUnavailable，成功时返回 AuthUser。
    """
    settings = settings or get_settings()

    # 1. 先确认服务端自己有没有配密钥。
    #    这一步必须在解析 token 之前做，否则“没配密钥”会被误判成“token 无效”。
    if not settings.jwt_configured:
        logger.error("L2D_AI_JWT_SECRET 未配置或短于 32 字符，拒绝所有聊天请求")
        raise AuthUnavailable("服务端尚未配置登录密钥，请联系站长")

    # 2. 做一点廉价的格式检查，避免把明显不是 JWT 的东西交给解析器。
    if not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 2:
        raise AuthError("登录凭证无效，请刷新页面")

    # 3. PyJWT 的核心调用。
    #    decode 会依次做：拆分三段 → 校验 header 算法 → 用密钥重算签名
    #    → 校验 iss/aud → 校验 exp/iat → 返回 payload 字典。
    #    任何一步失败都会抛 InvalidTokenError 的子类。
    try:
        payload = pyjwt.decode(
            token,
            settings.l2d_ai_jwt_secret,
            algorithms=["HS256"],
            audience=settings.l2d_ai_jwt_audience,
            issuer=settings.l2d_ai_jwt_issuer,
            leeway=settings.l2d_ai_jwt_leeway_seconds,
            options={
                # 这些字段一个都不能少。比如缺少 exp 的 token 永不过期，
                # 一旦泄露就能被无限期使用。
                "require": ["iss", "aud", "exp", "iat", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except pyjwt.ExpiredSignatureError:
        # 过期是最常见的情况，给用户一句能指导行动的提示。
        raise AuthError("登录状态已过期，请刷新页面") from None
    except pyjwt.InvalidTokenError as exc:
        # 不把 exc 的详细内容返回给浏览器：攻击者可能利用报错信息试探。
        # 日志里只记异常类型，绝不记录 token 本身。
        logger.info("JWT 校验失败：%s", type(exc).__name__)
        raise AuthError("登录凭证无效，请刷新页面") from None

    # 4. 取出用户 ID。
    #    JSON 里的数字会被解析成 int，但插件用字符串发送（"sub": "12"），
    #    所以这里统一转成 int。bool 是 int 的子类，要单独排除，
    #    否则 True 会被当成用户 ID 1。
    raw_sub = payload.get("sub")
    if isinstance(raw_sub, bool):
        raise AuthError("登录凭证无效，请刷新页面")
    try:
        user_id = int(raw_sub)
    except (TypeError, ValueError):
        raise AuthError("登录凭证无效，请刷新页面") from None
    if user_id <= 0:
        raise AuthError("登录凭证无效，请刷新页面")

    # 5. admin 只接受布尔 true。
    #    字符串 "true"、"1" 一律当作普通用户；权限判断宁严勿松。
    is_admin = payload.get("admin") is True

    # 6. name 只用于显示和日志，不能参与任何权限判断。
    #    即使 WordPress 那边改名了，也不影响用户 ID 的稳定性。
    raw_name = payload.get("name")
    name = str(raw_name).strip()[:100] if raw_name else ""

    return AuthUser(id=user_id, name=name, is_admin=is_admin)
