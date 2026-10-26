"""JWT 校验的单元测试。

测试原则：每个测试只验证一个行为，失败时能立刻知道是哪条规则被破坏了。
这里不启动 Web 服务，直接调用 verify_token，速度快、定位准。
"""

from __future__ import annotations

import time

import jwt
import pytest

from app.auth import AuthError, AuthUnavailable, verify_token
from app.config import Settings

# 测试专用密钥。它不是真实密钥，也不参与任何生产配置。
SECRET = "test-secret-" + "x" * 52


def make_settings(**overrides: object) -> Settings:
    """构造一份测试配置。

    显式传入的值优先级最高，会覆盖 .env 和环境变量，
    所以测试不会受开发者本机配置影响。
    """
    values: dict[str, object] = {
        "l2d_ai_jwt_secret": SECRET,
        "l2d_ai_jwt_issuer": "l2d-ai-wordpress",
        "l2d_ai_jwt_audience": "l2d-ai-backend",
        "l2d_ai_jwt_leeway_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def make_token(
    *,
    secret: str = SECRET,
    sub: object = "42",
    admin: object = False,
    iss: str = "l2d-ai-wordpress",
    aud: str = "l2d-ai-backend",
    exp_delta: int = 900,
    include_exp: bool = True,
    algorithm: str = "HS256",
) -> str:
    """签一个测试 token。

    PyJWT 的 encode 和 WordPress 插件的 encode_jwt 做的是同一件事：
    Base64URL(header) + "." + Base64URL(payload) + "." + HMAC-SHA256 签名。
    """
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "name": "测试用户",
        "admin": admin,
        "iat": now,
    }
    if include_exp:
        payload["exp"] = now + exp_delta

    key: str | None = secret if algorithm != "none" else None
    return jwt.encode(payload, key, algorithm=algorithm)


def test_valid_token_returns_user() -> None:
    user = verify_token(make_token(), make_settings())

    assert user.id == 42
    assert user.name == "测试用户"
    assert user.is_admin is False


def test_admin_claim_is_boolean_only() -> None:
    assert verify_token(make_token(admin=True), make_settings()).is_admin is True

    # 字符串 "true" 不能被当成管理员，否则只要改一个类型就能提权。
    assert verify_token(make_token(admin="true"), make_settings()).is_admin is False


def test_expired_token_is_rejected() -> None:
    token = make_token(exp_delta=-10)

    with pytest.raises(AuthError) as excinfo:
        verify_token(token, make_settings())

    assert "过期" in excinfo.value.message


def test_missing_exp_is_rejected() -> None:
    # 没有 exp 的 token 永不过期，一旦泄露后果很严重，必须拒绝。
    token = make_token(include_exp=False)

    with pytest.raises(AuthError):
        verify_token(token, make_settings())


def test_wrong_secret_is_rejected() -> None:
    token = make_token(secret="another-secret-" + "y" * 50)

    with pytest.raises(AuthError):
        verify_token(token, make_settings())


def test_wrong_audience_is_rejected() -> None:
    token = make_token(aud="some-other-service")

    with pytest.raises(AuthError):
        verify_token(token, make_settings())


def test_wrong_issuer_is_rejected() -> None:
    token = make_token(iss="evil-issuer")

    with pytest.raises(AuthError):
        verify_token(token, make_settings())


def test_alg_none_is_rejected() -> None:
    # 经典的 JWT 攻击：把 alg 改成 none，去掉签名。
    # 只允许 HS256 的 decode 会直接拒绝。
    token = make_token(algorithm="none")

    with pytest.raises(AuthError):
        verify_token(token, make_settings())


def test_tampered_payload_is_rejected() -> None:
    token = make_token()
    header, payload, signature = token.split(".")
    # 把 payload 换成一个“管理员”版本，但保留原签名。
    forged = jwt.encode(
        {
            "iss": "l2d-ai-wordpress",
            "aud": "l2d-ai-backend",
            "sub": "42",
            "admin": True,
            "iat": int(time.time()),
            "exp": int(time.time()) + 900,
        },
        "wrong-secret-" + "z" * 55,
        algorithm="HS256",
    )
    forged = f"{header}.{forged.split('.')[1]}.{signature}"

    with pytest.raises(AuthError):
        verify_token(forged, make_settings())


def test_bad_subject_is_rejected() -> None:
    for bad in ("abc", "0", "-1", True, None):
        with pytest.raises(AuthError):
            verify_token(make_token(sub=bad), make_settings())


def test_short_or_missing_secret_fails_closed() -> None:
    token = make_token()

    with pytest.raises(AuthUnavailable):
        verify_token(token, make_settings(l2d_ai_jwt_secret="too-short"))

    with pytest.raises(AuthUnavailable):
        verify_token(token, make_settings(l2d_ai_jwt_secret=""))


def test_garbage_token_is_rejected() -> None:
    for bad in ("", "abc", "a.b", "a.b.c.d", "x" * 5000):
        with pytest.raises(AuthError):
            verify_token(bad, make_settings())
