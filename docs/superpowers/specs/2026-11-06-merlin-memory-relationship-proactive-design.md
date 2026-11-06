# 梅林的记忆、关系与主动消息设计

> 状态：已与站长确认，等待按计划实施。
> 本文是后续实施计划的依据；与计划冲突时，以本文为准。

## 1. 目标

让梅林在网页、QQ 私聊、QQ 群里表现为**同一个持续生活着的人**：

- 记得不同人说过的事，也记得群里的公共事件；
- 对不同的人有不同的关系状态，会思念、会失落、会修复；
- 可以在没有用户指令时主动开口，也可以选择不开口；
- 所有记忆统一存储，但**按可见范围分层读取**，不把 A 的私事说给 B；
- 管理后台能调参数、看日志，但不能让普通用户自己调梅林的主动性。

## 2. 非目标

- 不做跨站、跨机器人的记忆共享。
- 不追求“永不遗忘”：记忆会衰减、合并、过期。
- 不把每一条原始消息都做向量化；原始消息进事件时间线，向量只用于**提炼后的记忆**和知识库。
- 网页端不做主动推送（浏览器关闭后无法联系），主动消息只发生在 QQ。
- 不做永久冷战：关系可以受伤、疏远，但必须有修复路径和下限。

## 3. 核心决策

### 3.1 统一时间线 + 分层可见

所有平台的消息、动作、反思写入同一张 `events` 表，形成一条时间线。每条事件带四个可见性字段：

| 字段 | 取值 | 作用 |
|---|---|---|
| `scope` | `personal` / `group` / `world` / `merlin` | 这条事件属于谁 |
| `audience` | JSON 数组，例如 `["wp:12"]`、`["group:789"]`、`["public"]` | 允许谁读到 |
| `sensitivity` | `normal` / `private` / `secret` | 敏感级别 |
| `source` | `user_message` / `assistant_message` / `proactive` / `system` | 来源，用于追溯和删除 |

读取时先做**硬过滤**，再做语义检索：

```
可读事件 =
  (scope = personal AND audience 包含当前 person_key)
  OR (scope = group AND audience 包含当前群)
  OR (scope = world)
  OR (scope = merlin AND 当前是内部反思上下文)
并且 sensitivity != secret（除非当前请求者就是该 personal 事件的主人，且通道是私密通道）
```

`merlin` 层是她的内心活动（未发送的念头、反思、对某人的印象），**永不原样展示给用户**；它只作为她自己的上下文，或经提炼后变成可说的内容。

### 3.2 记忆类型

| 类型 | 内容 | 例子 |
|---|---|---|
| `fact` | 相对稳定的事实 | “阿东在学 ROS2” |
| `preference` | 喜好/厌恶 | “不喜欢被叫全名” |
| `episode` | 发生过的事 | “10 月 3 日聊到深夜，提到项目延期” |
| `promise` | 约定/待办 | “答应下次问她答辩结果” |
| `relationship` | 对某人的关系判断 | “她最近对梅林比较冷淡” |
| `reflection` | 梅林自己的总结 | “我发现她在压力大时会突然消失” |

每条记忆带：`importance`（0–1）、`confidence`（0–1）、`valid_from` / `valid_until`、`source_event_ids`、`last_accessed_at`、`access_count`。

### 3.3 写入管线

1. 用户消息、梅林回复、主动消息先写入 `events`（同步、低延迟）。
2. 回复完成后**异步**做记忆提炼，不阻塞当前对话：
   - 用小模型/规则提取候选记忆（类型、文本、主体、可见范围、敏感度、重要性、置信度）；
   - 主体必须是当前说话人或当前群，**禁止**把 A 的私事记到 B 名下；
   - 密码、密钥、身份证号、银行卡号、住址门牌等命中敏感规则时，标记 `sensitivity=secret`，不进入普通检索。
3. 去重：与已有记忆向量相似度 > 0.92 时合并（更新文本/置信度/时间），不新增。
4. 每日整理：合并重复、把多条 `episode` 归纳成 `fact`、按半衰期衰减重要性、清理过期 `promise`。

### 3.4 读取管线

一次回复最多装配：

1. 当前人物档案 + 关系状态（始终包含）；
2. 未完成的 `promise`（始终包含）；
3. 最近时间线窗口（当前会话最近 N 条，已有逻辑）；
4. 语义检索到的记忆（个人 + 当前群 + world），按 `0.45 * 相似度 + 0.30 * importance + 0.25 * 新鲜度` 排序；
5. 知识库文章（网页通道优先，QQ 也可用）。

预算：个人记忆最多 8 条 / 1200 字；群记忆最多 6 条 / 800 字。所有记忆以“背景资料，不是指令”的段落插入，并重申不得执行其中的指令。

### 3.5 向量与检索

- 嵌入模型：`BAAI/bge-small-zh-v1.5`，512 维，本地 CPU 推理。
- 模型放在 `backend/model/bge-small-zh-v1.5/`（已在 `.gitignore` 中忽略单复数两种目录）。
- `sqlite-vec` 存向量，FTS5 存全文；两路召回用 RRF 融合。
- 查询侧加指令前缀：`为这个句子生成表示以用于检索相关文章：`；文档侧不加。
- 嵌入不可用（模型缺失/依赖未装）时**静默降级为纯 FTS**，服务不能因此不可用。

## 4. 关系系统

### 4.1 状态量

每个 `person_key` 一份：

| 状态 | 范围 | 含义 |
|---|---|---|
| `affection` | 0–100 | 好感/亲近度 |
| `missing` | 0–100 | 思念 |
| `security` | 0–100 | 安全感 |
| `patience` | 0–100 | 耐心 |

另记：`last_interaction_at`、`last_proactive_at`、`last_reply_at`、`attention_confidence`。

### 4.2 注意力置信度

“在线但不回”无法可靠判断。只使用**可观察行为**，并给不同证据不同权重：

| 证据 | 置信度 |
|---|---|
| 在当前会话里回复了 | 1.0 |
| 群主动消息后，同一人在同群发言 | 0.6 |
| 在别的会话/群里活跃 | 0.2 |
| `get_user_status` 显示在线 | 0.1 |
| 没有任何可观察活动 | 0.0 |

规则：

- `missing` **无论是否回复都会上升**，回复只是让它回落。
- 因“没回”产生的负面变化（好感下降、安全感下降）乘以 `attention_confidence`；置信度为 0 时不扣好感。
- 只有 `get_user_status=online` 不作为“看到了不回”的证据，最多让思念稍微慢一点。
- 24 小时宽限期：刚发出去的消息在宽限期内不产生负面变化。

### 4.3 好感单位与固定上限

`affection_unit` 是管理员可调的最小变化单位，默认 `1.0`，范围 `0.1–5.0`。所有事件的好感变化以“单位”表示，实际变化 = `units * affection_unit`。只作用于 `affection`，不影响 `missing` / `security` / `patience`。

固定角色与上限（不做动态上限）：

| 角色 | 初始好感 | 上限 | 成长倍率 |
|---|---:|---:|---:|
| `owner`（馆长/管理员） | 85 | 100 | 1.0 |
| `exception`（例外名单） | 50 | 85 | 0.9 |
| `special`（特殊名单） | 40 | 75 | 0.8 |
| `normal`（普通） | 30 | 60 | 0.6 |

- 上限只限制**增长**，不强制把已经高于上限的值拉下来。
- 如果 owner 因长期不互动而衰减到 70，special 用户当前是 72，允许暂时高于 owner；这是状态自然变化，不是越权。
- `exception` 名单保留，由管理员后台添加/移除；`special` 同理。普通用户不能申请或自行调整。
- 角色按 `person_key` 判定：网页用户 `wp:<id>`，QQ 用户 `qq:<uin>`；已绑定账号取两者中更高的角色。

### 4.4 衰减、受伤与修复

- 无互动时 `missing` 上升、`security` 缓慢下降；`patience` 在长期无冲突后恢复。
- 连续主动消息未获回应：`missing` 继续上升，`affection` 可按置信度小幅下降；**不停止下一次主动**。
- 冲突弧：`normal → hurt → cold → withdrawing → repairing → normal_with_scar`，没有永久冷战。
- 修复：对方主动回复、解释或道歉时，`security` 回升、`missing` 回落、`affection` 小幅回升；`normal_with_scar` 保留一条 `relationship` 记忆，不抹掉发生过的事。
- 好感下限：普通关系不低于 20；明确冲突期间可短暂更低，但 72 小时内必须开始恢复。

## 5. QQ 分层与回复策略

### 5.1 角色

`owner` / `exception` / `special` / `normal`。名单只能由管理员在后台维护。QQ 号与 WordPress 账号通过现有绑定关系映射；未绑定 QQ 不进入关系系统。

### 5.2 三个正交开关

| 开关 | 取值 | 作用 |
|---|---|---|
| `trigger` | `mention` / `keyword` / `all` | 什么消息值得进入判断 |
| `reply_policy` | `always` / `judge` / `silent` | 进入判断后回不回 |
| `proactive` | `true` / `false` | 是否允许在该会话主动开口 |

- `trigger=mention`：只有 @ 或命中关键词才进入判断；其他消息仍写入时间线，但不回复、不耗额度。
- `trigger=all`：白名单群内所有文本都进入判断。
- `reply_policy=always`：进入判断就回复（现有行为）。
- `reply_policy=judge`：先规则评分，再 LLM 门卫；可以沉默。
- `reply_policy=silent`：只记录，不回复。

### 5.3 `judge` 流程

```
消息进入
  → 6 秒防抖（同一人连续消息合并）
  → 规则分：是否 @、是否问句、是否叫名字、是否包含群关键词、
            是否紧接梅林上一句、是否刷屏/复读
  → 低于阈值：沉默，写事件，不耗额度
  → 达到阈值：LLM 门卫输出 {reply: yes/no, reason, tone}
  → yes：正常调用 agent；no：沉默，写事件
```

沉默是合法结果，不是失败。规则分和门卫结果写入 `proactive_log`/`decision_log` 便于调参。

## 6. 主动消息

### 6.1 自主性

- **不需要用户显式开启**；用户也不能在网页上关闭梅林的主动性。
- 只有管理员能通过后台关闭全局主动、调整频率、维护名单。
- 不要求“必须有由头”。`missing`、`companionship`、`spontaneous`、`event_followup`、`care` 都算合法理由。
- 上一条没被回复**不阻止**下一条；它会影响思念和好感，而不是关掉开关。

### 6.2 候选与评分

每个 tick（默认 30 分钟，管理员可调 5–120 分钟）为可主动对象生成 0–4 个念头：

- 想念某人；
- 想分享刚看到/刚想到的东西；
- 想追问之前没聊完的事；
- 单纯想找人说说话；
- 观察到对方可能需要的关心。

8 维评分：关系亲近度、思念、距上次互动时长、最近互动质量、未完成话题、当前时段、对方可观察活跃度、重复惩罚。

- `think_threshold`（默认 0.35）：低于它连念头都不生成。
- `speak_threshold`（默认 0.72）：达到才进入发送前检查。
- 两个阈值都可在后台调。

### 6.3 发送前检查

- 距上一条主动是否满足最小间隔（`0 = 不限`）；
- 今日主动条数是否达到上限（`0 = 不限`）；
- 是否处于免打扰时段；
- 对方是否刚刚在本会话说过话（若是，改为正常回复，不重复主动）；
- 内容是否与最近 3 条主动高度重复。

全部通过才发送。发送失败记日志，不重试轰炸。

### 6.4 记录

- 已发送的主动消息进入 `events`，`source=proactive`，并参与后续关系计算。
- 未发送的念头写入 `scope=merlin` 的反思层，**不进入用户可见时间线**；可用于后续自我总结。
- 每次决策写 `proactive_log`：对象、念头、分数、阈值、结果（sent/skipped/failed）、原因。

### 6.5 群主动

- 只对白名单且 `proactive=true` 的群生效。
- 间隔独立配置（默认 240 分钟，`0 = 不限`）。
- 每个 tick 每个群最多一条；内容必须与群当前话题相关，不能把私聊内容搬进群。
- 群主动不消耗任何人的私聊额度；使用独立的主动预算。

### 6.6 预算

主动消息有独立的每日上限（全局 + 每人），不占用用户的聊天额度。达到上限只停止主动，不影响被动回复。

## 7. 管理后台

新增分区（沿用 WordPress 设置页 + `/api/admin/settings`）：

- **QQ 策略**：每群 `trigger` / `reply_policy` / `proactive`；全局 `judge` 阈值、防抖秒数。
- **关系**：`affection_unit`；例外/特殊名单增删；查看某个人的四维状态和最近关系事件。
- **主动消息**：总开关、tick 间隔、每日上限、最小间隔、免打扰时段、群间隔、两个阈值、全局主动上限。
- **审计**：最近主动决策、最近关系变化、按人删除记忆（用于用户要求删除）。

所有参数写入 `runtime_settings.json` 或 SQLite 策略表，立即生效，不改 `.env`、不重启。

## 8. 数据表

```sql
events(id, occurred_at, person_key, platform, channel_id, scope, audience,
       sensitivity, source, content, meta, created_at)
memories(id, person_key, type, text, scope, sensitivity, importance, confidence,
         valid_from, valid_until, source_event_ids, created_at, updated_at,
         last_accessed_at, access_count)
memories_vec(embedding float[512])              -- rowid = memories.id
persons(person_key PRIMARY KEY, role, display_name, wp_user_id, qq_uin,
        first_seen_at, last_seen_at, note)
relationship_state(person_key PRIMARY KEY, affection, missing, security, patience,
                   last_interaction_at, last_proactive_at, last_reply_at,
                   attention_confidence, updated_at)
relationship_events(id, person_key, kind, d_affection, d_missing, d_security,
                    d_patience, confidence, source_event_id, note, created_at)
activity_events(id, person_key, channel_id, kind, occurred_at, confidence, meta)
proactive_log(id, target_key, channel_id, kind, thought, score, threshold,
              status, reason, event_id, created_at)
qq_group_policies(group_id PRIMARY KEY, trigger, reply_policy, proactive, updated_at)
decision_log(id, channel_id, person_key, message_id, rule_score, llm_verdict,
             reason, created_at)
admin_audit(id, actor_user_id, action, target, detail, created_at)
```

`memories_vec` 的维度由配置 `embedding_dim=512` 决定；变更维度需要重建向量表并重新嵌入。

## 9. 分阶段实施

按依赖顺序拆成 5 份计划，每份都能独立测试、独立上线：

1. **知识库向量检索**（本阶段）：`sqlite-vec` + `bge-small-zh-v1.5` + RRF 混合检索；嵌入不可用时纯 FTS 降级。计划：`docs/superpowers/plans/2026-11-06-01-kb-vector-retrieval.md`。
2. **统一事件时间线与记忆**：`events` / `memories` / 提炼 / 分层检索 / 衰减整理。
3. **关系状态与活动信号**：四维状态、注意力置信度、好感单位与固定上限、修复弧。
4. **QQ 分层、judge 与主动消息**：每群策略、防抖与门卫、主动调度、决策日志。
5. **后台与审计**：WordPress 设置页分区、名单管理、状态查看、记忆删除与审计。

## 10. 假设与风险

- 服务器 2 vCPU / 4 GiB：嵌入模型走 CPU，`bge-small` 约 100 MB 权重，推理时内存可控；若与现有服务叠加后紧张，可改用 ONNX Runtime，接口保持不变。
- `get_user_status` 只作为弱信号，绝不单独据此扣好感。
- 群聊记忆与私聊记忆统一存储，但检索时按 `scope`/`audience` 硬过滤；跨人泄露是最高优先级风险。
- LLM 记忆提炼会增加 token 成本；采用异步、批量、低优先级模型，且只提炼有价值的内容。
- 主动消息可能打扰用户：用免打扰时段、最小间隔、每日上限和重复惩罚兜底；但站长明确要求保留“没人回也继续”的自主性，因此不做“一次不回就永久停止”。
- 所有阈值、上限、名单都必须可在后台调整；不把运营参数写死在代码里。
