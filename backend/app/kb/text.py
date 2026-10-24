"""文本处理：HTML 清洗、中文分词、切块、摘要。

为什么需要这一层：
- WordPress REST 返回的是 HTML，直接丢给检索会把标签也当成内容；
- SQLite FTS5 的默认分词器按空格切词，中文句子没有空格，需要先用 jieba 分词；
- 整篇文章太长，检索时返回整篇既浪费 token 又不够精准，所以要先切块。

这个文件里的函数都是“纯函数”：输入字符串/HTML，输出字符串/列表，
不读写数据库，也不调用网络，方便单独测试。
"""

from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# jieba 0.42.1 在 Python 3.13 上会触发几个 SyntaxWarning（正则里的转义写法），
# 不影响功能。这里在导入时临时忽略，避免服务启动日志被刷屏。
with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    import jieba

# jieba 第一次分词时会构建前缀词典并打印日志；调到 WARNING 让命令行输出干净。
jieba.setLogLevel(logging.WARNING)

# 一些领域词，避免被 jieba 切得太碎。加到自定义词典后，
# “LangChain”“Live2D”等词会作为一个整体进入索引。
for _word in (
    "dds",
    "ros2",
    "live2d",
    "wordpress",
    "deepseek",
    "langchain",
    "sqlite",
    "fts5",
    "docker",
    "nginx",
    "api",
    "agent",
    "cubism",
):
    jieba.add_word(_word)


# 常见停用词：这些词几乎每篇文章都有，放进索引只会降低检索质量。
_STOPWORDS = {
    "的", "了", "和", "是", "在", "我", "有", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "那", "这个", "那个", "可以", "我们",
    "他们", "以及", "但是", "因为", "所以", "如果", "然后", "还是", "已经",
    # 博客问答里非常常见、但几乎不携带检索信息的词。
    # 去掉它们可以减少“问什么都命中同一篇”的噪声。
    "内容", "问题", "文章", "方法", "使用", "进行", "情况", "时候", "现在",
    "这里", "那里", "什么", "怎么", "为什么", "如何", "介绍", "分享", "记录",
    "教程", "相关", "需要", "支持", "通过", "由于", "对于", "关于", "一些",
    "这些", "那些", "站内", "博客", "有没有", "推荐", "一下", "知道", "告诉",
    "请问", "帮我", "找找", "看看", "存在",
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "was",
    "were", "it", "this", "that", "for", "on", "with", "as", "at", "by",
}

# 用于从 jieba 结果中提取英文/数字词。
_ASCII_WORD_RE = re.compile(r"[a-z0-9_+#.]{2,}")
_CHINESE_RE = re.compile(r"^[一-鿿]+$")


@dataclass
class ImageRef:
    """文章里的一张图片。

    url 是图片的真实地址（已经处理过相对路径）；
    alt/title/caption 是 WordPress 或主题自带的说明，通常可能为空。
    """

    url: str
    alt: str = ""
    title: str = ""
    caption: str = ""


def html_to_text(html: str) -> str:
    """把 WordPress 返回的 HTML 正文变成适合检索的纯文本。

    BeautifulSoup 的 `get_text()` 会丢掉标签，但不会自动处理换行：
    连续两个 <p> 会粘成一行。因此这里先：
    1. 删除脚本/样式等不可见内容；
    2. 把 <br> 换成换行；
    3. 用分隔符提取文本，再压缩多余空白。

    注意：这里故意保留代码块里的文字，因为技术博客的代码也是检索内容。
    """

    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 这些标签的内容对读者不可见，或与文章正文无关。
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    # <br> 是行内换行，get_text 不会自动补换行。
    for br in soup.find_all("br"):
        br.replace_with("\n")

    text = soup.get_text("\n")

    # 去掉行首尾空白，压缩行内连续空格；空行最多保留一行。
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t ]+", " ", line).strip()
        if line:
            lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def extract_images(html: str, base_url: str = "") -> list[ImageRef]:
    """从文章 HTML 中提取图片真实地址。

    这里有一个实际踩过的坑：Argon 主题启用了懒加载，
    `<img src="...">` 里放的是 1×1 的占位图，真实地址在 `data-original`。
    所以查找顺序是：
        data-original → data-src → data-lazy-src → srcset → src

    同时会跳过 `data:` 内联占位图、SVG 图标和非 http(s) 地址。
    结果按 URL 去重，避免同一张图在文章里出现两次就索引两次。
    """

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    images: list[ImageRef] = []
    seen: set[str] = set()

    for img in soup.find_all("img"):
        real_url = ""

        # 1. Argon / 常见懒加载插件使用的属性。
        for attr in ("data-original", "data-src", "data-lazy-src"):
            value = (img.get(attr) or "").strip()
            if value and not value.startswith("data:"):
                real_url = value
                break

        # 2. 有些主题只在 srcset 里放真实地址（通常最后一个是最大尺寸）。
        if not real_url:
            for attr in ("data-srcset", "srcset"):
                value = (img.get(attr) or "").strip()
                if not value:
                    continue
                candidates = [part.strip().split(" ")[0] for part in value.split(",")]
                candidates = [item for item in candidates if item and not item.startswith("data:")]
                if candidates:
                    real_url = candidates[-1]
                    break

        # 3. 最后才看普通的 src。
        if not real_url:
            value = (img.get("src") or "").strip()
            if value and not value.startswith("data:"):
                real_url = value

        if not real_url:
            continue

        # 相对路径转绝对路径；只接受 http/https。
        real_url = urljoin(base_url, real_url)
        parsed = urlparse(real_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue

        # DeepSeek 视觉接口支持 JPEG/PNG/GIF/WebP，不支持 SVG。
        # 这里先跳过 SVG，避免同步时每次都下载后才失败。
        if parsed.path.lower().endswith(".svg"):
            continue

        if real_url in seen:
            continue
        seen.add(real_url)

        caption = ""
        figure = img.find_parent("figure")
        if figure is not None:
            figcaption = figure.find("figcaption")
            if figcaption is not None:
                caption = figcaption.get_text(" ", strip=True)

        images.append(
            ImageRef(
                url=real_url,
                alt=(img.get("alt") or "").strip(),
                title=(img.get("title") or "").strip(),
                caption=caption,
            )
        )

    return images


def chunk_text(text: str, size: int = 600, overlap: int = 80) -> list[str]:
    """把长文章切成带重叠的小块。

    为什么不直接按固定长度切：
    固定切法会把一句话、一个代码块从中间截断，检索出来的片段很难读。
    这里会优先在段落/句号处断开，并让相邻块保留一小段重叠，
    避免答案刚好落在切口另一侧。

    参数：
        size:    每块最多多少字符；
        overlap: 相邻块重叠多少字符（不能超过 size 的一半）。
    """

    text = text.strip()
    if not text:
        return []
    if size <= 0:
        raise ValueError("chunk size 必须大于 0")

    overlap = max(0, min(overlap, size // 2))
    chunks: list[str] = []
    start = 0
    total = len(text)

    while start < total:
        end = min(start + size, total)

        # 在目标位置附近找一个自然边界，避免把句子从中间切开。
        if end < total:
            window_start = start + int(size * 0.55)
            for separator in ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? "):
                position = text.rfind(separator, window_start, end)
                if position != -1:
                    end = position + len(separator)
                    break

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)

        if end >= total:
            break

        # 下一块从当前结尾往前退 overlap 个字符，形成重叠。
        next_start = end - overlap
        if next_start <= start:
            # 防止 size/overlap 配置异常导致死循环。
            next_start = start + 1
        start = next_start

    return chunks


def tokenize(text: str) -> list[str]:
    """把一段中文/英文文本切成适合 FTS5 的词。

    FTS5 的 unicode61 分词器只按空格/标点切分，对中文等于整段算一个词。
    所以我们在写入索引前先用 jieba 切好，再用空格连接；
    查询时对用户的问题做同样处理，两边才能对上。

    规则：
    - 中文词：长度 >= 2，过滤常见停用词；
    - 英文/数字：长度 >= 2，保留 c++、c#、ros2 这类技术词；
    - 去重并保持出现顺序。
    """

    if not text:
        return []

    tokens: list[str] = []
    for raw in jieba.cut_for_search(text.lower()):
        item = raw.strip()
        if not item:
            continue

        if _CHINESE_RE.match(item):
            if len(item) >= 2 and item not in _STOPWORDS:
                tokens.append(item)
            continue

        # jieba 对英文一般会整词返回；这里再用正则兜底，去掉附带的标点。
        for part in _ASCII_WORD_RE.findall(item):
            part = part.strip("._#")
            if len(part) >= 2 and part not in _STOPWORDS:
                tokens.append(part)

    # dict.fromkeys 既去重又保留顺序（Python 3.7+ 的 dict 有序）。
    return list(dict.fromkeys(tokens))


def build_match_query(tokens: list[str], operator: str = "OR") -> str:
    """把词列表拼成 FTS5 的 MATCH 表达式。

    生成的形式是：
        tokens : ("docker" OR "容器" OR "网络")

    - `tokens :` 表示只在 tokens 这一列里匹配；
    - OR 让任意一个词命中即可，再由 BM25 按命中程度排序；
    - AND 要求所有词都出现，适合先做一次“严格检索”；
    - 每个词加双引号，避免 + # 等字符被 FTS5 当成语法。
    """

    unique = list(dict.fromkeys(tokens))[:12]  # 限制词数，防止查询过长
    if not unique:
        return ""

    operator = "AND" if operator.upper() == "AND" else "OR"
    quoted = [f'"{token}"' for token in unique]
    return "tokens : (" + f" {operator} ".join(quoted) + ")"


def make_excerpt(text: str, tokens: list[str], width: int = 120) -> str:
    """从正文里截取包含关键词的一小段，作为来源摘要。

    为什么不是“找到第一个关键词就截”：
    技术博客里 `dds` 很可能先出现在代码里，最早命中位置截出来的会是一段代码，
    对阅读来源卡片的人没有帮助。这里改为在所有命中位置开窗，
    选“窗口内包含的不同关键词最多”的那一段；数量相同则选靠前的，
    让摘要尽量同时覆盖用户问的几个词。

    如果找不到关键词（例如 FTS5 命中的是词形变化），
    就退回成正文开头，保证卡片上总有内容可展示。
    """

    clean = re.sub(r"\s+", " ", text).strip()
    if not clean:
        return ""

    lower = clean.lower()
    positions: list[int] = []
    for token in dict.fromkeys(tokens):
        if not token:
            continue
        start = 0
        # 每个词最多收集 40 个位置，避免极端长文本把计算量放大。
        for _ in range(40):
            position = lower.find(token, start)
            if position < 0:
                break
            positions.append(position)
            start = position + len(token)

    if not positions:
        excerpt = clean[:width]
        return excerpt + ("…" if len(clean) > width else "")

    unique_tokens = list(dict.fromkeys(token for token in tokens if token))
    best_start = 0
    best_score: tuple[int, int] | None = None

    for position in positions:
        start = max(0, position - width // 3)
        end = min(len(clean), start + width)
        # 如果接近结尾，向前挪一点，保证摘要长度尽量接近 width。
        start = max(0, end - width)

        window = lower[start:end]
        distinct = sum(1 for token in unique_tokens if token in window)
        # 先比“覆盖了几个不同关键词”，再比“谁更靠前”。
        score = (distinct, -start)
        if best_score is None or score > best_score:
            best_score = score
            best_start = start

    end = min(len(clean), best_start + width)
    prefix = "…" if best_start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    return prefix + clean[best_start:end] + suffix
