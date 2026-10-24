"""应用配置。

这里用 pydantic-settings 读取配置，它是 Pydantic 官方提供的“带校验的配置类”：
- 字段名会自动映射到环境变量（大小写不敏感），例如 `deepseek_api_key`
  对应环境变量 `DEEPSEEK_API_KEY`；
- 也可以从 `.env` 文件读取（见 SettingsConfigDict 的 env_file 参数）；
- 类型注解会做转换和校验，例如字符串 "60" 会自动变成 float 60.0。

这样做的好处：配置项集中、有类型提示、启动时就能发现配置错误，
而不是等到调用 API 时才报一个看不懂的错。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务全部配置项。"""

    # ------------------------------------------------------------------
    # DeepSeek 模型配置
    # ------------------------------------------------------------------
    # ChatDeepSeek 默认会读取环境变量 DEEPSEEK_API_KEY；
    # 这里显式声明，是为了统一从 .env 读取，并在启动时就能发现“没配 Key”。
    deepseek_api_key: str = ""

    # DeepSeek 提供 OpenAI 兼容接口。LangChain 的 ChatDeepSeek 内部也是
    # 通过这个 base_url 发请求，所以换代理/网关时只需要改这里。
    deepseek_base_url: str = "https://api.deepseek.com"

    # 模型名：deepseek-chat（通用对话）或 deepseek-reasoner（推理模型）。
    deepseek_model: str = "deepseek-chat"

    # ------------------------------------------------------------------
    # 服务行为
    # ------------------------------------------------------------------
    # 允许跨域的前端地址，逗号分隔。
    # 本地开发需要它；生产环境用 Nginx 同域反代时通常用不到。
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 强制 mock：为 True 时不调用真实模型，用假数据流联调前端。
    mock_mode: bool = False

    # 单次请求超时（秒）。传给 ChatDeepSeek 的 timeout 参数。
    request_timeout: float = 60.0

    # 送给模型的历史消息上限。太长会浪费 token，太短会丢上下文。
    agent_max_history: int = 20

    # SettingsConfigDict 是 pydantic-settings 的配置：
    # - env_file=".env"：从当前工作目录的 .env 读取；
    # - env_file_encoding="utf-8"：中文注释/值不乱码；
    # - extra="ignore"：.env 里多出来的变量不会导致启动失败。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def mock_enabled(self) -> bool:
        """没有 API Key 或显式开启 MOCK_MODE 时，进入 mock 模式。"""
        return self.mock_mode or not self.deepseek_api_key.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的字符串拆成列表，CORS 中间件需要 list[str]。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例。

    `@lru_cache` 会缓存函数第一次的返回值：后续调用不再重复读 .env、
    也不重复做校验。测试时如果改了环境变量，需要调用
    `get_settings.cache_clear()` 让缓存失效。
    """
    return Settings()
