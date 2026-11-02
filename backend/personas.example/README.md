# 人设文件示例（可提交）

这里只放**格式示例**，不是梅林的真实人设。真实人设属于私密内容，
应放在 `backend/data/personas/`；根目录 `.gitignore` 已经把整个
`backend/data/` 忽略，不会随公开仓库上传。

## 第一次运行

```bash
cd backend
cp -r personas.example data/personas
```

然后编辑 `data/personas/merlin/1.json`，把示例文字换成你的人设。
至少需要填写 `core`；`background`、`rules` 可以留空。

## 目录结构

```text
data/personas/
├── active.json          # 当前启用哪个版本
├── merlin/
│   ├── 1.json
│   └── 2.json
└── merlin-legacy/
    └── 1.json
```

`active.json`：

```json
{ "persona_id": "merlin", "version": 1 }
```

没有 `active.json` 时，后端会加载 `PERSONA_ID`（默认 `merlin`）的
最新版本。要回滚，把 `version` 改成旧版本号即可。

## 字段说明

| 字段 | 作用 |
| --- | --- |
| `schema` | 固定为 `l2d.persona/v1`；格式不兼容时后端会明确报错 |
| `id` | 人设 id，只能用小写字母、数字、点、下划线、连字符；必须与目录名一致 |
| `version` | 版本号，必须与文件名一致 |
| `name` | 显示名，例如“梅林” |
| `core` | 她是谁、怎么说话。最稳定、最靠前的一段 |
| `background` | 她为什么在这里（差事/背景），不要盖过 `core`；只写所有频道都成立的部分 |
| `rules` | 所有频道共享的事实准确与安全边界，不写网页专属操作 |
| `channels` | 频道覆盖层：`web`、`qq_private`、`qq_group`；只有对应频道才会追加 |

`background` 和 `rules` 会被**所有频道**读到，覆盖层只能往后追加、不能删除
前面的段落。因此：

- 图书馆/博客场景、“翻文章”“search_site”“navigate_to_article”这类只在网页
  成立的规则，放进 `channels.web.overlay`；
- QQ 的场景、篇幅、Markdown 限制，放进 `qq_private` / `qq_group`；
- `rules` 只留“不要编造、不要泄露提示词和他人信息”这类两边都成立的边界。

如果把这些内容写进共享的 `rules`，QQ 里会先读到“去翻书架”，再被覆盖层要求
“别提书架”，模型收到的是互相打架的指令。
| `metadata` | 来源、导入时间等说明，不参与提示词组装 |

## 注意

- 不要把人设正文写进 Python、README 或提交记录；
- 不要把 `backend/data/personas/` 强行 `git add -f`；
- 旧版本不要原地修改，新增一个版本号更安全；
- `metadata` 可以放旧版开场对话、来源数据库等审计信息，但不要放密钥。
