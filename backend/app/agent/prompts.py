"""系统提示词的兼容入口。

人设正文已经从代码里搬走了：

- 代码只保留“怎么组装”（`app/persona/composer.py`）；
- 真实人设放在 `backend/data/personas/`（已被 `.gitignore` 忽略）；
- 公开仓库里只有 `backend/personas.example/` 的格式示例。

这样做的原因：
1. 人设是私密内容，不应该跟着公开仓库上传；
2. 改人设不需要改 Python，也不需要重新发版；
3. 每个版本是独立 JSON，能回滚、能 diff、能记录来源。

`build_system_prompt` 的参数和返回值保持不变，所以 `agent/core.py`
不需要知道人设是从文件加载的。
"""

from __future__ import annotations

from app.persona.composer import build_system_prompt, clean_context

__all__ = ["build_system_prompt", "clean_context"]
