"""管理员设置接口。

WordPress 后台的“Live2D 小助手”设置页会在服务器端签一个 admin JWT，
再调用这里的 GET/PUT。浏览器不会直接拿到后端的任何密钥。

为什么不让 WordPress 直接改 `.env`：
- `.env` 是部署配置，格式简单但并发写入容易损坏；
- 改完必须重启进程才生效，体验很差；
- WordPress 运行账户（www）通常没有 `/opt/l2d-ai` 的写权限。

所以运营参数单独存到 `data/runtime_settings.json`，由后端负责校验和落盘。
端口、API Key 这类部署参数仍然只读展示，避免“看起来能改、其实没生效”。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_admin
from app.auth import AuthUser
from app.config import get_settings
from app.runtime_settings import get_runtime_store
from app.schemas import RuntimeSettingsUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


def _listen_port() -> int | None:
    """从当前进程的启动参数里读出 uvicorn 的监听端口。

    systemd 的 ExecStart 通常是：

        .../uvicorn app.main:app --host 127.0.0.1 --port 18080

    `/proc/self/cmdline` 用 NUL 分隔参数，Linux 上可直接读取。
    读不到时返回 None（例如 Windows 本地开发），前端显示“未知”即可，
    不影响任何功能。
    """
    try:
        raw = Path("/proc/self/cmdline").read_bytes()
    except OSError:
        return None

    args = [item.decode("utf-8", "ignore") for item in raw.split(b"\0") if item]
    for index, arg in enumerate(args):
        if arg in {"--port", "-p"} and index + 1 < len(args):
            try:
                return int(args[index + 1])
            except ValueError:
                return None
        if arg.startswith("--port="):
            try:
                return int(arg.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _snapshot() -> dict[str, object]:
    """当前设置 + 默认值 + 只读的运行信息。"""
    settings = get_settings()
    store = get_runtime_store()
    runtime = store.load()

    return {
        "settings": asdict(runtime),
        "defaults": asdict(store.defaults),
        "backend": {
            # 只告诉管理员“有没有 Key”，绝不返回 Key 的内容。
            "api_key_configured": bool(settings.deepseek_api_key.strip()),
            "mock": settings.mock_enabled,
            "listen_port": _listen_port(),
            "quota_timezone": settings.quota_timezone,
            "jwt_configured": settings.jwt_configured,
        },
    }


@router.get("/admin/settings")
async def read_settings(user: AuthUser = Depends(require_admin)) -> dict[str, object]:
    """读取当前生效的运营参数。"""
    return _snapshot()


@router.put("/admin/settings")
async def update_settings(
    payload: RuntimeSettingsUpdate,
    user: AuthUser = Depends(require_admin),
) -> dict[str, object]:
    """更新运营参数。

    Pydantic 已经做了第一层校验（类型、范围、字段名）；
    RuntimeSettingsStore 会再做一次同样的校验，防止有人绕过接口直接改文件。
    """
    patch = payload.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="没有需要更新的设置项")

    try:
        updated = get_runtime_store().update(patch)
    except ValueError as exc:
        # 这类错误是管理员输入问题，422 比 500 更准确。
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # 只记录“谁改了哪些键”，不记录值，避免日志里出现敏感配置。
    logger.info("管理员更新设置 user=%s keys=%s", user.id, sorted(patch))
    result = _snapshot()
    result["updated"] = asdict(updated)
    return result
