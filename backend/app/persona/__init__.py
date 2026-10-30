"""人设运行时：版本文件、提示词组装与后续的状态/记忆扩展点。

公开仓库里只放“格式”和“示例”，真实人设正文放在 `backend/data/personas/`。
该目录已被根目录的 `.gitignore` 整体忽略，不会随 Git 上传。
"""

from app.persona.composer import build_system_prompt, clean_context
from app.persona.models import ChannelOverlay, PersonaVersion
from app.persona.store import (
    ActivePersonaRef,
    PersonaError,
    PersonaNotFound,
    PersonaStore,
    get_persona_store,
)

__all__ = [
    "ActivePersonaRef",
    "ChannelOverlay",
    "PersonaError",
    "PersonaNotFound",
    "PersonaStore",
    "PersonaVersion",
    "build_system_prompt",
    "clean_context",
    "get_persona_store",
]
