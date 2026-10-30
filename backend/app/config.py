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
    # Agent 防失控（第二步新增）
    # ------------------------------------------------------------------
    # 一轮对话里最多允许模型“思考”几次。
    # 每次“模型 → 工具 → 模型”的往返都会让这个数字 +1。
    # 模型偶尔会陷入“再查一次”的循环；有上限才能保证最坏情况也有尽头。
    # 典型问答只需 2 次（一次检索 + 一次总结），6 次已经留足余量。
    agent_max_model_calls: int = 6

    # 一轮对话里最多执行几个工具。
    # 和模型调用上限分开设置：一次模型回复可能同时要求调用多个工具。
    agent_max_tool_calls: int = 6

    # 一轮对话从开始到结束最多允许的秒数。
    # 调用次数上限管的是“循环多少次”，这里管的是“卡住多久”，
    # 两者互补：工具本身卡死时，调用次数并不会增加。
    agent_max_seconds: float = 90.0

    # ------------------------------------------------------------------
    # 登录凭证（与 WordPress 插件共享）
    # ------------------------------------------------------------------
    # 必须和 wp-config.php 里的 L2D_AI_JWT_SECRET 完全相同，至少 32 个字符。
    # 这个值只用于“验证 token 是不是 WordPress 签发的”，不会发给前端。
    l2d_ai_jwt_secret: str = ""

    # 下面两项是 JWT 的标准声明（claim）：
    # iss = issuer（谁签发），aud = audience（签给谁用）。
    # 验证时必须和插件里的常量一致，否则说明 token 来自别处。
    l2d_ai_jwt_issuer: str = "l2d-ai-wordpress"
    l2d_ai_jwt_audience: str = "l2d-ai-backend"

    # token 有效期（秒）。插件签发 900 秒；这里只用于文档说明和健康检查，
    # PyJWT 会直接读取 token 里的 exp，不需要我们重新计算。
    l2d_ai_jwt_ttl_seconds: int = 900

    # 允许的时钟偏差（秒）。服务器和 WordPress 可能差几秒，
    # 留一点余量可以避免“刚签发就过期”的误判。
    l2d_ai_jwt_leeway_seconds: int = 30

    # ------------------------------------------------------------------
    # 每日聊天额度（第二步新增）
    # ------------------------------------------------------------------
    # 每个普通用户每天最多发几条消息。管理员不受限制。
    # 0 表示普通用户完全不能聊天（管理员仍可用）。
    daily_quota: int = 20

    # 会员每天最多发几条消息。会员仍然限额，只是比普通用户高。
    # 0 同样表示会员也不能聊天（极少见，但保持和其他额度项一致）。
    member_daily_quota: int = 60

    # 全站普通用户每天合计最多几条消息。0 表示不限。
    # 管理员既不计入个人额度，也不计入全站额度——
    # 这个总上限是给公开访客兜底的“总电闸”，避免异常流量烧掉 API 账单。
    global_daily_quota: int = 0

    # 额度计数数据库。和知识库分开存放：
    # 知识库删掉可以重建，额度记录删掉会“重置所有人用量”，两者生命周期不同。
    quota_db_path: str = "data/quota.sqlite3"

    # 单个用户额外额度的存量上限。额外额度不会每天刷新，只增不减，
    # 所以设一个上限，防止管理员发放时多打一个零（比如想发 100 发成 10000）。
    credit_max_per_user: int = 10_000

    # 管理员在 WP 后台修改的运营参数（额度、调用上限、模型名）保存在这里。
    # 文件不存在时全部使用本文件的默认值。
    runtime_settings_path: str = "data/runtime_settings.json"

    # ------------------------------------------------------------------
    # 人设文件
    # ------------------------------------------------------------------
    # 人设正文属于“私密内容”，放在 data/ 下（整个 data/ 已被 .gitignore 忽略），
    # 不随公开仓库上传。目录结构见 backend/personas.example/README.md。
    #
    # 为什么不用数据库：
    # 人设版本更新频率低、需要人工审阅和 diff，JSON 文件比数据库更直观；
    # 以后的关系状态/记忆才需要 SQLite。
    persona_dir: str = "data/personas"

    # 没有 active.json 时，默认加载这个人设的最新版本。
    persona_id: str = "merlin"

    # ------------------------------------------------------------------
    # QQ 通道（实验性）
    # ------------------------------------------------------------------
    # 默认关闭。即使打开，也只处理白名单群里的 @ 消息；
    # 没有白名单时一条都不回，避免误接入陌生群。
    qq_enabled: bool = False

    # NapCat 的 OneBot 11 HTTP API 地址。只应该监听 127.0.0.1。
    qq_onebot_base_url: str = "http://127.0.0.1:3000"
    qq_onebot_token: str = ""

    # NapCat 往 /api/channels/qq/events 发事件时携带的 token。
    # 为空且不是 mock 模式时，接口会拒绝服务，避免公网裸奔。
    qq_event_token: str = ""

    # 逗号分隔的群号/QQ 号。群白名单为空时，不响应任何群。
    qq_allowed_groups: str = ""
    qq_admin_users: str = ""

    # at：只有 @ 机器人时回复；prefix：消息以 qq_prefix 开头；
    # all：白名单群里的所有文本消息都回复（不建议在活跃群使用）。
    qq_trigger: str = "at"
    qq_prefix: str = "梅林"

    # 每个群+用户最多保留多少轮对话（1 轮 = 用户+助手两条）。
    qq_history_turns: int = 8

    # 本地没有 NapCat 时，把出站消息写到 outbox，而不是发 HTTP 请求。
    # 这不是生产模式，只用于 Windows 本地联调和测试。
    qq_mock_mode: bool = False
    qq_mock_outbox: str = "data/qq_mock_outbox.jsonl"

    # 按哪个时区判断“新的一天”。用北京时间，符合博客读者的直觉。
    # 依赖 Python 3.9+ 的 zoneinfo；Windows 上需要 tzdata（项目已安装）。
    quota_timezone: str = "Asia/Shanghai"

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
    def jwt_configured(self) -> bool:
        """JWT 密钥是否可用。

        为什么要求至少 32 个字符：
        HS256 的安全性完全依赖密钥的随机性。太短的密钥可以被暴力猜测，
        攻击者就能自己伪造任意用户的 token（包括管理员）。
        插件端也做了同样的长度检查，两边保持一致。
        """
        return len(self.l2d_ai_jwt_secret.strip()) >= 32

    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的字符串拆成列表，CORS 中间件需要 list[str]。"""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def qq_allowed_group_ids(self) -> set[int]:
        """群白名单。

        解析失败或为空都返回空集合；适配器看到空集合时不会回复任何群。
        这是“默认拒绝”的安全默认值：配置写错时宁可不工作，也不要误回陌生群。
        """
        return _parse_int_csv(self.qq_allowed_groups)

    @property
    def qq_admin_user_ids(self) -> set[int]:
        """可以直接按管理员身份对话的 QQ 号。"""
        return _parse_int_csv(self.qq_admin_users)


def _parse_int_csv(value: str) -> set[int]:
    """把逗号分隔的 ID 字符串转成整数集合，忽略空白和非法项。

    群号/QQ 号都是纯数字。配置里出现 `123, abc, 456` 时，
    只保留 123 和 456，并在日志/测试里能看出 abc 被忽略，
    而不是让整个服务因为一个手滑的字符启动失败。
    """
    result: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item.isdigit():
            result.add(int(item))
    return result


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例。

    `@lru_cache` 会缓存函数第一次的返回值：后续调用不再重复读 .env、
    也不重复做校验。测试时如果改了环境变量，需要调用
    `get_settings.cache_clear()` 让缓存失效。
    """
    return Settings()
