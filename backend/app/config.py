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

    # 模型名：deepseek-flash 是当前的 Flash 模型（DeepSeek-V4.1-Flash），
    # 同时支持普通对话、工具调用和图片理解，所以聊天与图片描述都用它。
    deepseek_model: str = "deepseek-flash"

    # DeepSeek V4.1 默认开启“思考模式”：回复前先生成一段思维链。
    # 对博客小助手来说，这会增加延迟和 token 消耗，而且思维链也不适合直接展示，
    # 因此默认关闭；如果以后要做复杂推理，可以把它设为 false。
    deepseek_disable_thinking: bool = True

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

    # ------------------------------------------------------------------
    # 站内知识库
    # ------------------------------------------------------------------
    # WordPress 站点地址，例如 https://374969782.xyz。
    # 同步脚本会依次尝试 /wp-json/、/index.php/wp-json/ 和 ?rest_route= 三种入口，
    # 兼容没有配置伪静态的站点。
    wp_site_url: str = ""

    # SQLite 数据库路径。相对路径以 backend/ 目录为基准，
    # 所以不管从哪个目录启动，都会落到 backend/data/ 下。
    kb_db_path: str = "data/kb.sqlite3"

    # 检索时最多返回几条来源。
    kb_top_k: int = 4

    # 文章切块大小（字符数）与相邻块的重叠量。
    # 块太大检索不精准，太小会丢上下文；600/80 对中文博客比较均衡。
    kb_chunk_size: int = 600
    kb_chunk_overlap: int = 80

    # 拉取 WordPress REST 接口的超时和 User-Agent。
    kb_request_timeout: float = 30.0
    kb_user_agent: str = "WordPressAssistant-KB/1.0"

    # 单张图片下载上限。超过就跳过，避免把几十 MB 的图片塞进请求。
    kb_image_max_bytes: int = 12_000_000

    # 是否用视觉模型给文章图片生成描述/OCR。
    vision_enabled: bool = True
    vision_model: str = "deepseek-flash"
    # low 更快更省；original 保留细节，适合截图和代码图。
    vision_detail: str = "original"
    vision_max_tokens: int = 800

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
