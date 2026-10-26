"""FastAPI 依赖（Dependency）。

====================================================================
Depends 是什么
====================================================================

FastAPI 的依赖注入可以理解为“路由函数声明自己需要什么，框架负责准备好”：

    async def chat(user: AuthUser = Depends(require_user)): ...

请求到达时，FastAPI 会先调用 `require_user()`：
- 它正常返回 → 把返回值作为 `user` 参数传进 chat；
- 它抛出 HTTPException → 直接结束请求，chat 根本不会执行。

好处是认证逻辑只写一遍，任何需要登录的接口加一行 `Depends(require_user)`
就自动受保护，不会出现“某个接口忘了检查”的漏洞。

====================================================================
HTTPBearer 是什么
====================================================================

`fastapi.security.HTTPBearer` 是 FastAPI 提供的安全方案解析器：
它从请求头 `Authorization: Bearer <token>` 里取出 `<token>`。

- `auto_error=False`：没有请求头时返回 None，而不是自动抛 403。
  这样我们可以自己决定状态码和错误文案（这里用更准确的 401）。
- 它还会把“需要 Bearer 认证”写进 OpenAPI 文档，`/docs` 页面会出现
  一个 Authorize 按钮，调试时可以直接粘贴 token。
"""

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth import AuthError, AuthUnavailable, AuthUser, verify_token

# auto_error=False 的返回值类型是“凭证或 None”。
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="WordPress 插件签发的短时 JWT（Authorization: Bearer <token>）",
)


async def require_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthUser:
    """要求请求携带有效 token，并返回对应用户。

    状态码约定：
    - 401：没登录、token 无效或过期。前端收到后应清缓存并提示刷新；
    - 503：服务端没配 JWT 密钥。这不是用户能解决的问题，但不能放行。
    """
    if credentials is None:
        # 401 表示“你是谁我不知道”；403 表示“我知道你是谁，但你没权限”。
        # 没带 token 属于前者。
        raise HTTPException(
            status_code=401,
            detail="请先登录后再聊天",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return verify_token(credentials.credentials)
    except AuthUnavailable as exc:
        # 服务端配置问题，返回 503 而不是 500：
        # 告诉调用方“现在暂时不可用”，而不是“代码崩了”。
        raise HTTPException(status_code=503, detail=exc.message) from None
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.message,
            headers=exc.headers,
        ) from None
