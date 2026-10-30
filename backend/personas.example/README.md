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
| `background` | 她为什么在这里（差事/背景），不要盖过 `core` |
| `rules` | 检索、跳转、安全边界 |
| `channels` | 频道覆盖层，例如 `web`、`qq_group`；P1 只用到 `web` |
| `metadata` | 来源、导入时间等说明，不参与提示词组装 |

## 注意

- 不要把人设正文写进 Python、README 或提交记录；
- 不要把 `backend/data/personas/` 强行 `git add -f`；
- 旧版本不要原地修改，新增一个版本号更安全；
- `metadata` 可以放旧版开场对话、来源数据库等审计信息，但不要放密钥。
