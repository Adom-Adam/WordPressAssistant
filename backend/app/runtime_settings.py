"""运行时可修改的设置。

====================================================================
为什么需要这一层
====================================================================

`.env` 里的配置有一个特点：**进程启动时读一次，之后不再变化**。
这对 API Key、端口这类“部署参数”很合适，但额度、调用上限、模型名
属于**运营参数**——管理员可能今天想改成 30 条，明天想改回 20 条。
如果每次都改 `.env` 再重启服务，既麻烦又会中断正在进行的对话。

所以这里单独放一层“运行时设置”：

    默认值（.env / Settings）
        │
        └── 被 data/runtime_settings.json 里的值覆盖
                    ▲
                    │
        WP 后台设置页 → /api/admin/settings

文件不存在、内容损坏或数值越界时，一律回退到 `.env` 默认值。
这样即使有人手工把 JSON 改坏，聊天接口也不会崩。

====================================================================
为什么用 JSON 而不是 SQLite
====================================================================

这里只有十几个键、写入频率极低（管理员点保存时），
JSON 文件配合“先写临时文件再 os.replace”的原子替换已经足够。
额度计数需要并发原子性，所以用 SQLite；两者场景不同。

`os.replace` 在同一个文件系统内是原子的：读者要么看到旧文件，
要么看到新文件，不会读到写了一半的内容。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """一次请求实际生效的运营参数。"""

    daily_quota: int
    global_daily_quota: int
    agent_max_model_calls: int
    agent_max_tool_calls: int
    agent_max_seconds: float
    model: str


# 每个字段的合法范围。管理员接口和读取文件时都会用同一套规则，
# 避免“接口校验了、手工改文件却没校验”的漏洞。
_INT_RANGES: dict[str, tuple[int, int]] = {
    "daily_quota": (0, 1000),
    "global_daily_quota": (0, 1_000_000),
    "agent_max_model_calls": (1, 20),
    "agent_max_tool_calls": (1, 20),
}
_SECONDS_RANGE = (5.0, 300.0)
# 模型名只允许常见字符，避免把换行/控制字符带进 HTTP 请求或日志。
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _defaults_from_settings(settings: Settings) -> RuntimeSettings:
    return RuntimeSettings(
        daily_quota=settings.daily_quota,
        global_daily_quota=settings.global_daily_quota,
        agent_max_model_calls=settings.agent_max_model_calls,
        agent_max_tool_calls=settings.agent_max_tool_calls,
        agent_max_seconds=settings.agent_max_seconds,
        model=settings.deepseek_model,
    )


def _validate(field: str, value: Any) -> Any:
    """校验单个字段，返回转换后的值；非法时抛 ValueError。

    这些错误会出现在管理员接口的 422 响应里，所以提示要能直接看懂。
    """
    if field in _INT_RANGES:
        if isinstance(value, bool):  # bool 是 int 的子类，要单独拒绝
            raise ValueError(f"{field} 必须是整数")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} 必须是整数") from None
        low, high = _INT_RANGES[field]
        if not low <= number <= high:
            raise ValueError(f"{field} 必须在 {low}~{high} 之间")
        return number

    if field == "agent_max_seconds":
        if isinstance(value, bool):
            raise ValueError("agent_max_seconds 必须是数字")
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            raise ValueError("agent_max_seconds 必须是数字") from None
        low, high = _SECONDS_RANGE
        if not low <= seconds <= high:
            raise ValueError(f"agent_max_seconds 必须在 {low:g}~{high:g} 之间")
        return seconds

    if field == "model":
        text = str(value or "").strip()
        if not _MODEL_PATTERN.fullmatch(text):
            raise ValueError("model 只能包含字母、数字、点、下划线、冒号或连字符")
        return text

    raise ValueError(f"未知设置项：{field}")


class RuntimeSettingsStore:
    """读写 runtime_settings.json。"""

    def __init__(self, path: str, settings: Settings) -> None:
        self.path = Path(path)
        self.defaults = _defaults_from_settings(settings)

        # 目录可能不存在（首次启动）。和额度库一样自动创建。
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RuntimeSettings:
        """读取当前生效值；文件缺失/损坏时返回默认值。"""
        if not self.path.exists():
            return self.defaults

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("运行时设置文件无法读取，使用默认值 path=%s", self.path)
            return self.defaults

        if not isinstance(raw, dict):
            logger.warning("运行时设置文件格式不是对象，使用默认值 path=%s", self.path)
            return self.defaults

        values = asdict(self.defaults)
        for field in values:
            if field not in raw:
                continue
            try:
                values[field] = _validate(field, raw[field])
            except ValueError as exc:
                # 单个字段非法只回退该字段，不丢弃其他合法设置。
                logger.warning("运行时设置项无效，使用默认值 field=%s error=%s", field, exc)

        return RuntimeSettings(**values)

    def update(self, patch: dict[str, Any]) -> RuntimeSettings:
        """合并并保存一批设置，返回保存后的完整值。

        只允许修改已知字段；任何一项非法都会整体拒绝，
        避免出现“一半保存成功、一半失败”的中间状态。
        """
        if not isinstance(patch, dict) or not patch:
            raise ValueError("没有需要更新的设置项")

        merged = asdict(self.load())
        for field, value in patch.items():
            if field not in merged:
                raise ValueError(f"未知设置项：{field}")
            merged[field] = _validate(field, value)

        self._write(merged)
        return RuntimeSettings(**merged)

    def _write(self, values: dict[str, Any]) -> None:
        """原子写入：先写临时文件，再 os.replace 覆盖正式文件。"""
        temp = self.path.with_name(f"{self.path.name}.tmp")
        try:
            temp.write_text(
                json.dumps(values, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp, self.path)
        finally:
            # 写入/替换失败时清理临时文件，避免留下垃圾。
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


@lru_cache(maxsize=1)
def get_runtime_store() -> RuntimeSettingsStore:
    """全局唯一的运行时设置存储。

    测试改了环境变量后需要调用 `get_runtime_store.cache_clear()`。
    """
    settings = get_settings()
    return RuntimeSettingsStore(settings.runtime_settings_path, settings)
