"""登录凭证（JWT）校验。

这个包只做一件事：把 WordPress 插件签发的 token 变成可信的用户身份。
额度、对话等业务逻辑都在别的模块，认证和业务互不纠缠。
"""

from app.auth.jwt import AuthError, AuthUser, AuthUnavailable, verify_token

__all__ = ["AuthError", "AuthUser", "AuthUnavailable", "verify_token"]
