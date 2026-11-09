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
| `dynamics` | 可选。心情、精力、回复概率、关系成长、节奏、安全词和隔离 namespace 的默认值；不写则用代码默认值 |
| `metadata` | 来源、导入时间等说明，不参与提示词组装 |

`dynamics.relationship` 里还有两项每轮对话的信号：`chat_affection_units`
（正常回复涨多少好感，单位是 `affection_unit`，默认 0.5）和 `chat_valence`
（心情变化，默认 0.03）。被忽略的一条有内容私聊不涨好感，只增加
`dynamics.reply.debt_increment` 的回复债。

主动消息和群内被指名的参数各有一个小节：

| 字段 | 默认 | 建议范围 | 作用 |
| --- | --- | --- | --- |
| `proactive.recent_active_minutes` | 10 | 0–60 | 对方刚在这个频道说过话时，这轮不主动打扰 |
| `proactive.repeat_similarity` | 0.6 | 0.4–0.8 | 越小越严格，和最近说过的话太像就这轮不发 |
| `proactive.ignore_grace_hours` | 24 | 12–72 | 主动消息多久没回应才开始结算 |
| `proactive.ignore_affection_points` | 0.2 | 0–1 | 确认被忽略时扣多少好感（会再乘注意力置信度） |
| `proactive.ignore_security_points` | 0.5 | 0–2 | 确认被忽略时扣多少安全感 |
| `proactive.compose_timeout_seconds` | 10 | 5–20 | 生成一条主动消息的超时，超时就这轮不发 |
| `group.reply_floor` | 0.90 | 0.85–0.98 | 群内被 @ 或点名时回复概率的下限 |

`ignore_grace_hours` 内的沉默**不扣分**：24 小时后如果对方一句话都没说，
置信度是 0；只有在别处活跃或回到这个私聊才按证据扣。调低这个值会让
梅林更“敏感”，不建议低于 12 小时。

`background` 和 `rules` 会被**所有频道**读到，覆盖层只能往后追加、不能删除
前面的段落。因此：

- 图书馆/博客场景、“翻文章”“search_site”“navigate_to_article”这类只在网页
  成立的规则，放进 `channels.web.overlay`；
- QQ 的场景、篇幅、Markdown 限制，放进 `qq_private` / `qq_group`；
- `rules` 只留“不要编造、不要泄露提示词和他人信息”这类两边都成立的边界。

如果把这些内容写进共享的 `rules`，QQ 里会先读到“去翻书架”，再被覆盖层要求
“别提书架”，模型收到的是互相打架的指令。

## 运行期调参：`dynamics.override.json`

版本文件是**不可变基线**；调参不要改版本文件，而是在同一目录放一个
`dynamics.override.json`：

```text
data/personas/merlin/
├── 1.json
└── dynamics.override.json
```

```json
{
  "reply": { "base": 0.9 },
  "relationship": {
    "growth_curve": 0.6,
    "roles": {
      "normal": { "initial": 10, "cap": 60, "growth": 0.6 }
    }
  }
}
```

- 加载时深合并到启用版本的 `dynamics` 上：只写要改的字段，没写的保持基线；
  列表（如 `hourly_curve`）整体替换，不逐项拼接。
- 合并后整体重新校验。字段名拼错、数值越界、JSON 语法错误，都会**整份回退**
  到基线并在服务端日志里告警，不会半份生效，也不会让人设加载失败。
- QQ 适配器每轮私聊重新读取人设，保存覆盖文件后下一轮立刻生效，不需要重启。
- 想恢复默认：删除 `dynamics.override.json` 即可。
- 覆盖文件按 `persona_id` 隔离，升级到新版本后仍然生效。

## 注意

- 不要把人设正文写进 Python、README 或提交记录；
- 不要把 `backend/data/personas/` 强行 `git add -f`；
- 旧版本不要原地修改，新增一个版本号更安全；
- `metadata` 可以放旧版开场对话、来源数据库等审计信息，但不要放密钥。
