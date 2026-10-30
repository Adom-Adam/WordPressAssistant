"""人设版本的加载与选择。

目录约定（默认 `backend/data/personas/`，已被 .gitignore 忽略）：

    data/personas/
    ├── active.json          # {"persona_id": "merlin", "version": 1}
    ├── merlin/
    │   ├── 1.json
    │   └── 2.json
    └── merlin-legacy/
        └── 1.json

为什么用“目录 + 版本文件”，而不是把提示词写回 Python 常量：
1. 提示词是私密内容，放在被忽略的 data/ 下，不会误提交；
2. 每次改人设都新增一个版本文件，旧版本可回滚、可对比；
3. 以后管理员在 WordPress 后台切换版本，只需要改 active.json。

注意：`active.json` 是“当前启用哪个版本”的指针，不是人设正文。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.config import get_settings
from app.persona.models import PersonaVersion

logger = logging.getLogger(__name__)

# 和 models.py 里的 id 规则保持一致。这里再检查一次，是因为目录名也会
# 被拼进路径；只靠 Pydantic 校验文件内容，挡不住“目录名本身是 ../”。
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class PersonaError(RuntimeError):
    """人设加载失败。消息会出现在服务端日志/管理员界面，不应包含正文。"""


class PersonaNotFound(PersonaError):
    """找不到可用的人设版本。"""


@dataclass(frozen=True, slots=True)
class ActivePersonaRef:
    """当前启用的人设坐标。"""

    persona_id: str
    version: int


class PersonaStore:
    """从磁盘读取人设版本。"""

    def __init__(self, root: Path, default_id: str) -> None:
        self.root = Path(root)
        self.default_id = default_id

    # ------------------------------------------------------------------
    # 路径安全
    # ------------------------------------------------------------------
    def _persona_dir(self, persona_id: str) -> Path:
        """把 persona_id 变成安全的子目录路径。

        即使调用方传入了 `../../etc`，也会在这里被拒绝，避免读站外文件。
        """
        if not _ID_PATTERN.fullmatch(persona_id):
            raise PersonaError(f"人设 id 不合法：{persona_id!r}")

        root = self.root.resolve()
        target = (root / persona_id).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            # 理论上正则已经挡住了，这里是第二道保险。
            raise PersonaError(f"人设路径越界：{persona_id!r}") from None
        return target

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def load(self, persona_id: str, version: int) -> PersonaVersion:
        """读取指定版本；文件缺失或格式错误时抛出 PersonaError。"""
        path = self._persona_dir(persona_id) / f"{version}.json"
        if not path.is_file():
            raise PersonaNotFound(f"人设文件不存在：{path}")

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PersonaError(f"人设文件无法读取：{path}（{exc}）") from None

        try:
            persona = PersonaVersion.model_validate(raw)
        except ValidationError as exc:
            # 不把完整正文打进日志；只记录字段路径和错误类型。
            details = "; ".join(
                f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
                for err in exc.errors()
            )
            raise PersonaError(f"人设文件格式不正确：{path}（{details}）") from None

        # 文件里的 id/version 必须和路径一致，避免“把 2.json 复制成 3.json”
        # 后日志里记录的版本和实际内容对不上。
        if persona.id != persona_id or persona.version != version:
            raise PersonaError(
                f"人设文件内容与路径不一致：{path}"
                f"（文件为 {persona.id}/v{persona.version}）"
            )
        return persona

    def list_versions(self, persona_id: str) -> list[int]:
        """列出某个人的所有版本号，按升序返回。"""
        directory = self._persona_dir(persona_id)
        if not directory.is_dir():
            return []

        versions: list[int] = []
        for path in directory.glob("*.json"):
            if path.stem.isdigit():
                versions.append(int(path.stem))
        return sorted(set(versions))

    # ------------------------------------------------------------------
    # 当前启用的版本
    # ------------------------------------------------------------------
    def active_ref(self) -> ActivePersonaRef:
        """读取 active.json；不存在时回退到 default_id 的最新版本。"""
        active_path = self.root / "active.json"
        if active_path.is_file():
            try:
                raw = json.loads(active_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PersonaError(
                    f"active.json 无法读取：{active_path}（{exc}）"
                ) from None

            if not isinstance(raw, dict):
                raise PersonaError("active.json 必须是 JSON 对象")

            persona_id = raw.get("persona_id")
            version = raw.get("version")
            if not isinstance(persona_id, str) or not isinstance(version, int):
                raise PersonaError(
                    "active.json 需要 persona_id（字符串）和 version（整数）"
                )
            if isinstance(version, bool) or version < 1:
                raise PersonaError("active.json 的 version 必须是正整数")
            return ActivePersonaRef(persona_id=persona_id, version=version)

        # 没有 active.json 时，用“默认人设的最新版本”。
        # 这方便本地第一次运行，也避免因为忘记建指针文件而完全不可用。
        versions = self.list_versions(self.default_id)
        if not versions:
            raise PersonaNotFound(
                f"没有找到人设：{self.root / self.default_id}。"
                "请从 backend/personas.example/ 复制一份到 backend/data/personas/，"
                "或设置 PERSONA_DIR 指向你的人设目录。"
            )
        return ActivePersonaRef(persona_id=self.default_id, version=versions[-1])

    def load_active(self) -> PersonaVersion:
        """加载当前启用的人设版本。"""
        ref = self.active_ref()
        return self.load(ref.persona_id, ref.version)


@lru_cache(maxsize=1)
def get_persona_store() -> PersonaStore:
    """全局唯一的人设存储。

    测试里改了 PERSONA_DIR 后需要调用 `get_persona_store.cache_clear()`。
    """
    settings = get_settings()
    return PersonaStore(Path(settings.persona_dir), settings.persona_id)
