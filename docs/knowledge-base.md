# 站内知识库：文章与图片检索

这一章讲清楚三件事：

1. 助手怎么知道博客里写了什么；
2. 图片为什么也能被搜到；
3. 回答下面的「来源序号」悬浮卡片和「带我去」按钮是怎么来的。

对应代码都在 [backend/app/kb/](../backend/app/kb/) 下，agent 接入点在
[backend/app/agent/tools.py](../backend/app/agent/tools.py) 和
[backend/app/agent/core.py](../backend/app/agent/core.py)。

---

## 1. 一句话原理

同步文章时，把每篇文章拆成小段，连同标题、链接、图片描述一起写进
**SQLite FTS5** 全文索引；用户提问时，先检索索引，再把命中的文章交给模型组织回答。

```
WordPress REST API
      │  拉取已发布文章（HTML）
      ▼
清洗正文 → 切块 → jieba 分词 ─┐
提取图片 → DeepSeek 视觉模型 ─┤
                              ▼
                    SQLite FTS5 索引（本地文件）
                              │  用户提问时检索
                              ▼
                    search_site 工具 → 模型回答
                              │
                              ├─ citations 事件 → [1] 序号 + 悬浮卡片
                              └─ action 事件    → 「带我去看看」按钮
```

**关键点：链接不是模型生成的。** 悬浮卡片里的标题和 URL 直接来自同步时保存的
WordPress 元数据，模型只负责组织语言，所以不会出现“编造一个不存在的链接”。

---

## 2. SQLite FTS5 是什么

可以把它想成一本字典的**索引页**：

- 普通数据库查询 `WHERE body LIKE '%Docker%'` 要逐行扫描全文，文章多了会慢；
- FTS5（Full-Text Search 5）会提前建立“词 → 出现在哪些文章”的倒排索引，
  查一个词时直接翻到对应位置，不需要扫全表。

SQLite 是 Python 标准库自带的单文件数据库，FTS5 是它的全文检索扩展。
好处是：

- 不用额外安装 Elasticsearch / Meilisearch；
- 整个知识库就是 `backend/data/kb.sqlite3` 一个文件，备份=复制文件；
- 几十到几万篇文章都够用。

### 中文为什么要先分词

FTS5 自带的 `unicode61` 分词器按空格和标点切词，对英文很好用，但中文句子没有空格，
它会把「机器人坐标系管理」当成一个超长的词。于是我们做了两层处理：

1. 写入前用 **jieba** 把中文切成词（`机器人 / 坐标系 / 管理`），用空格拼成 `tokens` 列；
2. 查询时对用户问题做同样的分词，两边才能对上。

索引表长这样（见 [db.py](../backend/app/kb/db.py)）：

```sql
CREATE VIRTUAL TABLE chunks USING fts5(
    post_id UNINDEXED,     -- 文章 ID
    chunk_index UNINDEXED, -- 第几块
    kind UNINDEXED,        -- text / image
    image_url UNINDEXED,   -- 图片块对应的图片地址
    url UNINDEXED,         -- 文章链接
    title,                 -- 标题（参与检索）
    body,                  -- 正文块（参与检索）
    tokens,                -- jieba 分词结果（真正被 MATCH 的列）
    tokenize = 'unicode61 remove_diacritics 2'
);
```

`UNINDEXED` 的意思是“只存不索引”。这些字段要在结果里带回去，但不需要被搜索。

### BM25 排序

FTS5 用 **BM25** 给结果打分：一个词在文章中越常见、在整个知识库里越稀有，
这篇文章就越相关。分数越小越相关，我们取负数变成“越大越好”，方便阅读。

因为真正被匹配的是 `tokens` 列，而且每个词只存一次，BM25 的分数差距很小，
主要受文章长度影响。为此 [search.py](../backend/app/kb/search.py) 又加了一层
**标题命中加成**：标题里出现的查询词越多，加分越高（最多 +1.5）。
这样搜「ROS2 节点」会优先返回《ROS2学习记录（2）-节点》，而不是碰巧提到“节点”的长文章。

---

## 3. 第一次建立索引

在 `backend/` 目录下：

```bash
# 确认 .env 里已经写好 WP_SITE_URL 和 DEEPSEEK_API_KEY
uv run python -m app.kb.sync
```

你会看到类似输出：

```text
共拉取到 20 篇文章，开始建立索引…
[1/20] 《世界，您好！》 正文 1 块，图片 0 张
...
同步完成：
  文章：20 篇
  文本块：147 个
  图片块：11 个（本次处理图片 11 张）
  图片描述缓存：11 条
  数据库：.../backend/data/kb.sqlite3
```

常用参数：

```bash
# 不调用视觉模型，只索引 alt / 图注 / 文件名（省钱、快）
uv run python -m app.kb.sync --no-vision

# 只同步前 3 篇，方便调试
uv run python -m app.kb.sync --limit 3

# 打印详细日志（包括图片描述用了多少 token）
uv run python -m app.kb.sync --verbose

# 临时换一个站点
uv run python -m app.kb.sync --site https://另一个域名
```

**每次发布或修改文章后，重新运行一次同步即可。** 默认是全量重建：
先清空文章和索引，再重新写入；图片描述有缓存，不会重复花钱。
纯文字索引几十篇文章通常几秒完成；第一次做图片理解会逐张调用视觉模型，时间更长。
同步过程中前端仍可提问（SQLite 开了 WAL），只是索引重建期间结果可能暂时不完整。

---

## 4. 图片是怎么变成可搜索文字的

FTS5 只能搜文字，看不懂像素。所以同步阶段会做一次“图片转文字”：

1. 从文章 HTML 里找图片。Argon 主题用了懒加载，`src` 是占位图，
   真实地址在 `data-original`，所以提取顺序是
   `data-original → data-src → data-lazy-src → srcset → src`；
2. 后端下载图片，用 base64 发给 `deepseek-flash`；
3. 模型返回 JSON：
   ```json
   {
     "description": "一张示意图，展示 visual geometry 与 collision geometry 的关系",
     "ocr_text": "frame visual geometry collision geometry",
     "keywords": ["URDF", "碰撞几何", "可视化几何", "frame"]
   }
   ```
4. 这些文字被写成一个 `kind='image'` 的检索块，同时保存图片地址。

用户搜「碰撞几何 示意图」时，就会命中这个图片块，来源悬浮卡片会显示对应图片。

### 缓存与费用

图片描述按图片 URL 的 SHA-256 缓存在 `images` 表里。
同一张图重复同步、或在多篇文章里出现，都只会调用一次视觉模型。

`deepseek-flash` 的价格大约是输入 1~2 元 / 百万 tokens、输出 4~8 元 / 百万 tokens
（以官方页面为准）。一张普通截图通常几百到一千多 tokens，
本项目 11 张图的总成本在一毛钱以内。

想彻底关闭图片理解：

```dotenv
VISION_ENABLED=false
```

关闭后图片仍会以 alt、图注、文件名进入索引，只是搜不到画面内容。

> 小坑：DeepSeek V4.1 默认开启思考模式。图片描述任务如果开着它，
> `max_tokens` 可能全被思维链吃掉，返回的 `content` 是空的。
> 代码里用 `extra_body={"thinking": {"type": "disabled"}}` 关闭，
> 聊天模型也做了同样处理（`DEEPSEEK_DISABLE_THINKING=true`）。

---

## 5. 检索策略

[search.py](../backend/app/kb/search.py) 按下面的顺序尝试，兼顾准确率和召回率：

1. **AND 检索**：所有关键词都必须出现，准确率最高；
2. **OR 兜底**：AND 没有结果时放宽，但要求命中足够比例的关键词
   （例如 3 个词至少命中 2 个），过滤掉“只碰巧命中一个词”的文章；
3. **LIKE 兜底**：分词后没有有效词，或 FTS 完全没命中时，用连续子串再查一次；
4. **按文章去重**：一篇文章可能命中多个块，只保留最相关的一条；
   如果同时命中了图片块，就把图片地址附加到结果上，显示缩略图；
5. **标题加成重排**：标题包含关键词的文章优先。

最终每条结果包含：

```json
{
  "post_id": 168,
  "title": "ROS2学习记录（12）-URDF：机器人建模方法",
  "url": "https://.../ros2...urdf/",
  "excerpt": "[图片] 一张示意图……",
  "kind": "image",
  "image_url": "https://.../image-1.png",
  "score": 32.49
}
```

### 为什么有些问题搜不到

- 站内确实没写过（例如「Docker 怎么安装」）：这是正确结果，助手会如实说没找到；
- 问法太口语化：`search_site` 的 description 已经提示模型提取核心技术词；
- 生僻的纯字母串：可能碰巧出现在代码里，属于边界情况，不影响正常提问。

---

## 6. Agent 怎么使用知识库

### 工具

[tools.py](../backend/app/agent/tools.py) 里有两个相关工具：

| 工具 | 作用 | 何时调用 |
| --- | --- | --- |
| `search_site(query, limit)` | 检索文章和图片描述 | 问站内内容、找文章、问技术概念 |
| `navigate_to_article(url, label)` | 生成跳转按钮 | 用户明确说“带我去 / 打开 / 跳转” |

两个工具都用了 LangChain 的 `response_format="content_and_artifact"`：

- **content**：给模型看的文字（标题、URL、摘要）；
- **artifact**：给程序用的结构化数据，模型看不到。

这样模型负责回答，`core.py` 负责把 artifact 转成前端事件，互不干扰。
`navigate_to_article` 会校验 URL 必须属于 `WP_SITE_URL` 同站，防止模型编造外链。

### 事件

`core.py` 用 `stream_mode=["messages", "updates"]` 同时订阅两种流：

- `messages`：模型 token 增量 → `delta` 事件（打字机效果）；
- `updates`：工具执行结果 → 从 `ToolMessage.artifact` 提取数据。

前端收到的事件长这样：

```text
data: {"type":"citations","citations":[{"title":"...","url":"...","post_id":168,"excerpt":"...","image_url":"...","kind":"image"}]}
data: {"type":"delta","content":"找到"}
data: {"type":"action","action":{"type":"navigate","url":"...","label":"打开 URDF 建模那篇"}}
data: {"type":"done"}
```

前端在回答气泡下方渲染 `[1] [2]` 小序号；悬浮、键盘聚焦或点按序号时，
弹出卡片显示标题、摘要和图片（有 `image_url` 才显示，没有就不占位）。
点击“打开文章”或跳转按钮都用**新标签页**打开，不会打断当前对话。

---

## 7. 配置项

全部在 `backend/.env`，模板见 [backend/.env.example](../backend/.env.example)。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `WP_SITE_URL` | 空 | WordPress 地址，同步必填 |
| `KB_DB_PATH` | `data/kb.sqlite3` | 索引文件路径（相对 `backend/`） |
| `KB_TOP_K` | `4` | 每次最多返回几篇 |
| `KB_CHUNK_SIZE` | `600` | 每块字符数 |
| `KB_CHUNK_OVERLAP` | `80` | 相邻块重叠字符数 |
| `KB_REQUEST_TIMEOUT` | `30` | 拉文章/下载图片超时秒数 |
| `KB_IMAGE_MAX_BYTES` | `12000000` | 单张图片大小上限 |
| `VISION_ENABLED` | `true` | 是否调用视觉模型 |
| `VISION_MODEL` | `deepseek-flash` | 图片描述模型 |
| `VISION_DETAIL` | `original` | `low` 更省，`original` 保留截图细节 |
| `VISION_MAX_TOKENS` | `800` | 图片描述输出上限 |

`backend/data/` 已加入 `.gitignore`：索引可以随时重建，不需要提交。
换机器或部署到服务器后，记得重新执行一次同步。

---

## 8. 常见问题

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| `找不到可用的 WordPress REST 接口` | 固定链接/伪静态配置不同，或 REST 被安全插件关闭 | 脚本会自动尝试三种入口；仍失败就检查插件设置，或手动访问 `https://域名/?rest_route=/wp/v2/posts` |
| 同步很慢 | 图片多，视觉模型逐张处理 | 先用 `--no-vision` 建文字索引；图片可后台慢慢补 |
| 图片没有缩略图 | 文章没图、图片被跳过或视觉描述失败 | 看 `--verbose` 日志；确认图片是 JPEG/PNG/GIF/WebP 且小于上限 |
| 图片描述是空的 | 思考模式占满了 max_tokens | 确认代码里关闭了 thinking（本项目已默认关闭） |
| 搜不到刚发布的文章 | 索引还没更新 | 重新运行 `uv run python -m app.kb.sync` |
| 助手引用了不存在的文章 | 不该发生：URL 来自索引 | 若出现，检查是否改了工具返回结构或提示词；来源卡片只认真实索引数据 |
| 控制台中文乱码 | Windows 控制台编码 | 运行前 `export PYTHONIOENCODING=utf-8` |
| jieba 的 SyntaxWarning | Python 3.13 + jieba 0.42.1 的正则写法 | 已在导入时忽略，不影响功能 |

---

## 9. 下一步可以做什么

- **增量同步**：文章上千篇后，按 `modified_gmt` 只更新改动过的文章；
- **WordPress 钩子**：发布/修改文章时由插件通知后端，免去手动同步；
- **向量检索**：用 embedding 做语义召回，与 FTS5 关键词检索混合排序；
- **上传图片找相似图**：用 CLIP 之类的视觉向量，把“以图搜图”也接进同一套来源浮层；
- **定时任务**：每天凌晨自动同步一次，保证新文章可被检索到。
