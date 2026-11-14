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

每个 `(persona_key, person_key)` 一份（默认按人设隔离，见第 10 节）：

| 状态 | 范围 | 时间尺度 | 含义 |
|---|---|---|---|
| `affection` | -100–100 | 天–周 | 好感/亲近度；0 = 陌生人 |
| `missing` | 0–100 | 天 | 思念 |
| `security` | 0–100 | 天 | 安全感 |
| `patience` | 0–100 | 天 | 耐心 |
| `valence` | -1–1 | 分钟–小时 | 心情好坏，向人格基线衰减 |
| `energy` | 0–1 | 小时 | 精力 = 本地时段曲线 − 短期疲劳（半衰期 60 分钟，可人设覆盖） |
| `reply_debt` | ≥0 | 8 小时半衰期 | 被忽略消息积累的回复意愿 |

另记：`last_interaction_at`、`last_proactive_at`、`last_reply_at`、`attention_confidence`、`updated_at`。

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
- 4b 只实现 1.0 / 0.6 / 0.2 三种证据（当前会话回复、群主动后同群发言、别处活跃）；`get_user_status` 在线 0.1 暂未接入。

### 4.3 好感单位、范围与成长

`affection` 范围改为 **-100 ~ +100**；`0` 代表陌生人，负值代表不喜欢，正值代表亲近。`affection_unit` 仍是管理员可调的最小变化单位，默认 `1.0`，范围 `0.1–5.0`。只作用于 `affection`，不影响 `missing` / `security` / `patience`，也不缩放 `valence` / `energy` / `reply_debt`——它们各有自己的参数。

固定角色与上限（不做动态上限）：

| 角色 | 初始好感 | 上限 | 成长倍率 |
|---|---:|---:|---:|
| `owner`（馆长/管理员） | 60 | 100 | 1.0 |
| `exception`（例外名单） | 0 | 85 | 0.9 |
| `special`（特殊名单） | 0 | 75 | 0.8 |
| `normal`（普通） | 0 | 60 | 0.6 |

- **除管理员外，所有角色都从 0 开始**；角色只决定上限和成长速度，不预设亲密关系。想让手动加名单的人一上来就更亲近，可在覆盖文件里单独调高该角色的 `initial`。
- 取消“普通关系不低于 20”的下限，允许长期负好感；负好感不等于永久敌对，修复事件仍能把它拉回来。
- 正面增长随当前好感递减：

```text
raw = units × affection_unit × role.growth
ratio = clamp(max(affection, 0) / cap, 0, 1)
gain_factor = (1 - ratio) ^ growth_curve
d_affection = raw × gain_factor
```

`growth_curve` 默认 `0.8`。0 附近接近全额，越接近上限越难升；负值区间 `gain_factor = 1`，所以道歉和关心仍然能修复，但单次与每日上限会防止一句道歉就洗白。

- 负面变化不乘成长倍率；严重事件在高好感时伤害更大，在已经很低时边际伤害略小，由 4.7 的事件类别和 `severity` 曲线控制。
- 上限只限制**增长**，不强制把已经高于上限的值拉下来。
- 老用户已有的好感不重置；新规则只影响新用户和后续变化。
- 如果 owner 因长期不互动而衰减到 70，special 用户当前是 72，允许暂时高于 owner；这是状态自然变化，不是越权。
- `exception` 名单保留，由管理员后台添加/移除；`special` 同理。普通用户不能申请或自行调整。
- 角色按 `person_key` 判定：网页用户 `wp:<id>`，QQ 用户 `qq:<uin>`；已绑定账号取两者中更高的角色。

### 4.4 衰减、受伤与修复

- 无互动时 `missing` 上升、`security` 缓慢下降；`patience` 在长期无冲突后恢复。
- 连续主动消息未获回应：`missing` 继续上升，`affection` 可按置信度小幅下降；**不停止下一次主动**。
- 冲突弧：`normal → hurt → cold → withdrawing → repairing → normal_with_scar`，没有永久冷战。
- 修复：对方主动回复、解释或道歉时，`security` 回升、`missing` 回落、`affection` 小幅回升；`normal_with_scar` 保留一条 `relationship` 记忆，不抹掉发生过的事。
- 好感下限为 **-100**；不再设置“普通关系不低于 20”。修复仍然通过道歉、解释和持续关心发生，但不设强制 72 小时恢复，避免机械感；冲突弧的终点是 `normal_with_scar`，不是永久冷战。
- **转移是确定性的**（不使用随机数）：同一种事件、同一份状态永远得到同样的下一步；活人感来自历史、好感、安全感和强度的差异。
- **进入冲突的门槛**：只有 `betrayal` / `hostility` 且 `confidence ≥ 0.6`、`intensity ≥ 0.5` 才算严重事件；`boundary` 需要 1 小时内 ≥3 次或 24 小时内 ≥5 次；`spam` 永不升级。低置信或低强度的负面事件只扣分，不把关系推进冲突。
- **最短停留**（人设参数 `dynamics.conflict.min_dwell_minutes`）：`hurt` 30 分钟、`cold` 6 小时、`withdrawing` / `repairing` 各 24 小时。最早 30 分钟后才接受真诚道歉进入 `repairing`，避免“5 秒内从受伤到原谅”。
- **无修复时的自然恶化**（只有时间参与，不新增定时任务，惰性结算）：`hurt` 满 24 小时（耐心 <50 时 12 小时）→ `cold`；`cold` 满 48 小时且安全感 <40 → `withdrawing`；`withdrawing` 满 14 天 → `normal_with_scar`，疤痕至少 0.4，绝不永久冷战；`repairing` 72 小时内没有新的正向事件 → `cold`。
- **修复门槛**：`hurt` 阶段一次够强的道歉即可；`cold` 还要安全感 ≥40，或累计正向事件达到 `repair_points_needed`（高好感 1 次、中好感 2 次、低好感 3 次）；`withdrawing` 不接受单独一句道歉，道歉之外 24 小时内还要有其他正向互动；`repairing` 满 24 小时、安全感 ≥60 且正向事件达标才回到 `normal_with_scar`。`repairing` 期间再次发生严重事件直接升级为 `withdrawing`。
- **伤疤**：严重事件留下 `scar_level += 0.3`，再次背叛 `+0.2`，按 14 天半衰期淡化；`scar_level` 使负面事件的 `d_affection` 额外乘 `1 + scar_level`；`normal_with_scar` 在疤痕 <0.1 且进入满 7 天后淡回 `normal`，事件与评估记录不删除。
- **阶段影响表达**：回复概率乘阶段系数（`normal` 1.0、`normal_with_scar` 0.98、`hurt` 0.85、`cold` 0.70、`withdrawing` 0.50、`repairing` 0.90），但危机、命令、绑定引导和额度提示永远绕过；提示词只给中文定性描述（“心里还有点受伤”“有些退缩”等），不出现阶段英文名或数值。阶段变化当轮即影响提示词，数值变化下一轮生效。
- **伤疤记忆推迟**：`memories` 表目前没有 `persona_key`，直接写跨人设的“伤疤记忆”会破坏 persona 隔离；当前用 `relationship_state`（`conflict_stage` / `stage_since` / `scar_level`）和 `appraisal_log` 承载，记忆接入另开任务。

### 4.5 affinity：从 affection 派生的慢变量

`affinity` 不单独存储，而是由 `affection` 换算出的回复概率倍率。因为 affection 现在可以为负，曲线必须跨过 0 平滑过渡：

```text
affinity = clamp(
  affinity_mid + affinity_amp × tanh(affection / affinity_scale),
  affinity_min,
  affinity_max
)
```

默认 `affinity_mid=1.50`、`affinity_amp=0.75`、`affinity_scale=60`、`affinity_min=0.10`、`affinity_max=2.20`，对应：

| affection | -100 | -60 | -20 | 0 | 20 | 40 | 60 | 85 | 100 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| affinity | 0.80 | 0.93 | 1.26 | 1.50 | 1.74 | 1.94 | 2.07 | 2.17 | 2.20 |

- 0 分对应 1.50，也就是“正常礼貌”：陌生人不会因为初始好感是 0 就先被打七折。疲惫、心情、耐心、冲突各有自己的乘数，压低回复率是它们的职责，不应该由“不认识”再重复扣一次。
- 负值把倍率压到 0.80 左右；真正讨厌一个人的低回复率来自负 mood、受伤/冷战阶段和模型给出的负 `reply_bias`，它们叠加起来才决定最终概率。
- 上限留到 2.20：好感到 100 时曲线仍在上升，不会在 40 分就饱和，后台显示的好感差才有意义。
- `tanh` 比原来的幂函数更适合跨 0 区间，且两端自然饱和，不需要分段。
- 角色**不直接乘** affinity；角色通过初始值、上限、成长倍率影响 affection 的轨迹，避免双重计算。
- affinity 只用于回复概率和关系语气提示，不覆盖危机/命令/绑定/额度。
- 主动消息评分不用 affinity，而用 `closeness_for()` 把好感换算成 0–1 的亲近度（0.5 = 刚认识）。两者分开之后，调回复概率不会把陌生人连带算成“亲密对象”。
- 后台修改 affection 后 affinity 立即随之变化；不维护第二份好感。

### 4.6 精力：时钟曲线 + 短期疲劳

`energy` 不是“聊天越多越累”的长期惩罚，而是“她现在几点、醒着没有”的状态：

```text
energy = clamp(hourly_curve[本地小时] - fatigue, 0, 1)
```

- `hourly_curve` 是 24 个小时值，按站点时区（`QUOTA_TIMEZONE`，默认 `Asia/Shanghai`）取本地小时；不使用 UTC 小时，也不依赖服务器本地时区。
- `fatigue` 只由正常回复累积（`fatigue_per_turn`，默认 `0.03`），按 `fatigue_half_life_minutes` 衰减，默认 **60 分钟**：一小时后剩 50%，两小时剩 25%。白天聊得再多，到深夜也已经接近 0。
- 不做跨天累积：白天聊天量不会让深夜更少回复；只有最近一小时内的连续聊天会叠加疲劳。
- `fatigue` 持久化在 `relationship_state`，随状态一起读写；`energy_delta_cap` 限制单次事件对疲劳的改变。
- 精力只读给模型：模型可以按精力调整语气和是否收尾，但不能修改精力值。
- `quiet_hours` 的语义定为**主动消息免打扰时段**，不是私聊回复禁令：用户主动找她时，仍然按精力、心情和概率回复；危机词永远绕过。4b 的主动调度只在时段外发送。若以后不做主动消息，可以直接删除这项配置，而不是让它继续当死参数。

### 4.7 模型参与关系判断（L2）

规则负责客观信号，模型负责“这一轮她怎么感受”。**每一轮私聊（防抖合并后）都评估一次**，不再只评估被概率忽略的回合；回复和沉默都先评估，再掷骰子。模型不直接写绝对值，也不直接输出概率倍率。

#### 输入与输出

- **输入**：合并后的用户文本 + 最近几条上下文 + 定性关系状态（关系阶段、心情、精力、本地时间、最近关系事件理由），不传裸数值。
- **输出**：

```json
{
  "event": "care | apology | flirt | shared_interest | smalltalk | neutral | tease | boundary | betrayal | hostility | spam",
  "intensity": 0.0,
  "confidence": 0.0,
  "reply_bias": 0.0,
  "reason": "不超过 80 字"
}
```

- `intensity` 范围 0–1，表示事件强度；`confidence` 范围 0–1；`reply_bias` 范围 -1–1，表示“这一句现在想不想接”。
- `reason` 只用于审计，不参与计算。
- 模型不能写 `affection` / `valence` / `energy` 的绝对值，不能写 `p0`，不能直接决定“回”或“不回”。

#### 代码映射

```text
d_affection = event_cap[event] × intensity × confidence
d_valence   = event_valence_cap[event] × intensity × confidence
```

事件上限示例（人设可调）：

| 事件 | 好感方向 | 说明 |
|---|---:|---|
| `apology` | +1.2 | 道歉、解释 |
| `care` | +1.0 | 关心、安慰 |
| `flirt` | +0.8 | 撒娇、暧昧 |
| `shared_interest` | +0.6 | 共同话题、认真交流 |
| `smalltalk` | +0.15 | 普通闲聊 |
| `neutral` | 0 | 中性 |
| `tease` | 见下 | 小冒犯、调侃；高好感可转正 |
| `boundary` | -1.0 | 越界、追问隐私、被拒绝后继续 |
| `betrayal` | -3.0 | 背叛、欺骗、严重失信 |
| `hostility` | -2.0 | 辱骂、攻击 |
| `spam` | -0.3 | 刷屏、复读 |

- `confidence < 0.5` 不落账。
- 每人每天模型带来的好感变化，正向最多 **+3**，负向最多 **-3**，分开计算。
- 心情单次变化最多 `valence_delta_cap`；角色上限、成长曲线、衰减仍然生效。
- 重复追问、被拒绝后继续、胁迫或性骚扰一律按 `boundary` / `betrayal` 处理，不能因为好感高就归为 `tease`。
- 写入 `relationship_events(kind="appraisal")`，保留 `confidence` 和 `reason`。

#### 高好感下的容忍与反噬

小冒犯和严重伤害要分开算：

```text
# 小冒犯 / 调侃：低好感偏负面，高好感可以中性甚至转正
tolerance = clamp(
  (affection - tease_negative_until) /
  (tease_positive_from - tease_negative_until),
  0, 1
)
base = lerp(-0.8, +0.6, tolerance)
d_affection = base × intensity × confidence
```

默认 `tease_negative_until=0`、`tease_positive_from=60`。所以 0 好感附近的小冒犯略扣分，60 以上可能变成“娇羞、嘴上嫌弃但不真生气”的正面事件。

```text
# 背叛 / 辱骂：好感越高，伤害越大
# event_cap 为负数（见上表），severity 只放大其绝对值
severity = 0.5 + 0.5 × clamp(affection / 100, 0, 1)
d_affection = event_cap[event] × intensity × confidence × severity
```

高好感不是“免死金牌”：越亲近的人背叛或辱骂，掉得越狠；同时可以额外扣 `security` / `patience`，由 3b 接入。低好感时伤害也不会归零，只是边际略小。

#### 时机、成本与失败

- **同步评估**：每一轮私聊在掷骰子前调用，默认超时 `1.2` 秒；超时或失败就只用规则，不改变状态。
- `reply_bias` 只影响**本轮**的 willingness；好感、心情、耐心、安全感的变化在掷骰子之后落账，影响下一轮，避免同一句话被算两次。
- **成本**：不扣用户额度，也不共用聊天全站额度；使用独立的全站 / 每人每日评估预算。超预算跳过评估，聊天不受影响。
- **失败**：解析失败、模型报错、预算耗尽都 fail-open，只记日志。
- **审计**：`appraisal_log` 保存原始输出、映射后的增量、实际落账值、耗时和回退原因；`decision_log` 保存 `p_rule`、`reply_bias`、最终 willingness。

## 5. QQ 分层与回复策略

### 5.1 角色

`owner` / `exception` / `special` / `normal`。名单只能由管理员在后台维护。

| 角色 | 好感上限 | 谁是这个角色 |
|---|---|---|
| `owner` | 100 | 站长自己，按管理员身份自动判定，后台改不动 |
| `exception` | 85 | 例外：后台手工指定 |
| `special` | 75 | 特别的人：未绑定的白名单 QQ 缺省就是这一档 |
| `normal` | 60 | 注册用户的默认值 |

改角色只改上限和之后的成长速度，**不会回收**已经攒下的好感——存量在读取时按上限夹紧，不做减法。

QQ 号与 WordPress 账号通过现有绑定关系映射成同一个 `person_key`；未绑定的 QQ 若在 QQ 白名单里，以 `qq:<uin>` 的身份进入关系系统（记忆、关系、主动消息、普通额度都有），只是没有博客账号。判定“这是个有身份的人”与“这个身份对应哪个账号”是两件事，名单只负责前者。

### 5.2 三个正交开关

| 开关 | 取值 | 作用 |
|---|---|---|
| `trigger` | `mention` / `keyword` / `all` | 什么消息值得进入判断 |
| `reply_policy` | `always` / `probabilistic` / `silent` | 进入判断后回不回 |
| `proactive` | `true` / `false` | 是否允许在该会话主动开口 |

- `trigger=mention`：只有 @ 或命中关键词才进入判断；其他消息仍写入时间线，但不回复、不耗额度。
- `trigger=all`：白名单群内所有文本都进入判断。
- `reply_policy=always`：进入判断就回复（现有行为）。
- `reply_policy=probabilistic`：按 5.3 的概率模型决定；私聊默认。
- `reply_policy=silent`：只记录，不回复。
- 群聊第一版仍用 `always` / `silent` + `trigger`；等私聊稳定后再把同一套概率模型接到群聊。

### 5.3 私聊：按意愿概率回复

私聊不再“进入判断就回复”，而是逐条计算回复概率。沉默是合法结果，不设白天/深夜连续沉默次数上限；只有斜杠命令、绑定引导、额度提示、危机/紧急求助强制回复。

#### 状态与信号

| 信号 | 来源 | 作用 |
|---|---|---|
| `willingness` | 规则看客观信号（问句、叫名字、紧接上一句、刷屏/复读），模型给 `reply_bias`，两者用 logit 合成 | 这一条想不想回 |
| `valence` | 模型评估 + 衰减 + 关系事件 | 这一阵心情好坏 |
| `energy` | 本地时段曲线 − 短期疲劳（半衰期 60 分钟） | 这一阵有没有精力 |
| `affinity` | 4.5 由 affection 派生 | 对这个人整体愿意投入多少 |
| `patience` | 冲突事件累积、无冲突时按天恢复 | 耐心低时压低回复意愿，最低乘 `0.80` |
| `security` | 背叛/冷淡下降、修复事件回升 | 安全感低时压低回复意愿，最低乘 `0.85` |
| `reply_debt` | 每忽略一轮有内容的私聊（防抖合并后）+`debt_increment`，按 `debt_half_life_hours` 衰减 | 被晾久了会更想回 |

规则层只负责客观信号，不做情感判断；模型评估给有界增量，避免和规则重复计分。心情既影响下一轮概率，也注入生成提示词影响语气。

#### 公式

规则先算 `p_rule`，再让模型的即时反应通过 logit 合成：

```text
z = log(p_rule / (1 - p_rule))
  + reply_bias_weight × confidence × reply_bias
willingness = sigmoid(z)
```

`reply_bias_weight` 默认 `1.0`，推荐范围 `0.5–1.5`。这个映射把模型的影响限制在有限 log-odds 内，模型不能输出“×10”这种失控倍率。

```text
patience_factor = patience_min + (1 - patience_min) × clamp(patience / 100, 0, 1)
security_factor = security_min + (1 - security_min) × clamp(security / 100, 0, 1)

p0 = clamp(
  reply_base × willingness × affinity
  × exp(0.8 × valence)
  × (0.55 + 0.45 × energy)
  × patience_factor
  × security_factor,
  probability_floor,
  probability_ceiling
)
p = p0 + (0.98 - p0) × (1 - exp(-debt_lambda × reply_debt))
```

默认 `reply_base=0.95`、`probability_floor=0.02`、`probability_ceiling=0.95`、`debt_increment=0.1`、`debt_half_life_hours=8`、`debt_lambda=1.5`、`patience_min=0.80`、`security_min=0.85`。所有参数进人设文件，不写死在代码里。耐心/安全感只做乘性下限，不会把概率压到 0；危机、命令和额度提示仍然绕过概率。

- 每一轮私聊（防抖合并后）都先做模型评估，再掷骰子；回复和沉默都评估，不再只评估被忽略的回合。
- `reply_bias` 只影响本轮 willingness；好感、心情、耐心、安全感的变化在掷骰子之后落账，影响下一轮。
- 深夜不禁止回复，只通过 energy 曲线和人格的时段参数降低 p0；深夜连续发消息会累积 `reply_debt`，仍然可能建立来回对话。
- 精力按 4.6 的本地时钟计算；白天的聊天量不会拖累深夜，只有最近一小时的连续聊天会叠加短期疲劳。
- 被概率忽略的消息仍然写时间线、加 `reply_debt`，并照常落账模型评估出的好感/心情变化；只是这一条不回。
- 评估调用不扣用户额度，计入独立的评估预算，避免刷屏时无限调用模型。
- 提问给较高的 willingness 基础分，但低好感对象不承诺必回。
- 未回复的内容会提高后续主动联系的候选分（第 6 节），不是“没回就结束”。
- 不设“连续沉默 N 次必须回复”的硬上限；给梅林按心情多次不回的空间。
- 危机词、斜杠命令、绑定引导、额度提示绕过概率，直接回复。
- 每条决策写 `decision_log`：`p_rule`、`reply_bias`、willingness、valence、energy、affinity、debt、p0、p、随机数、结果、原因。

### 5.4 私聊节奏：防抖、打断、多气泡

- **防抖**：首条到达后等 1.5 秒，新消息重置计时，总等待上限 4 秒；同一人的连续消息合并成一轮理解/回复。群聊仍用较长的防抖（默认 6 秒），后续阶段再统一。
- **打断**：生成中收到新消息就取消当前生成。第一条气泡尚未发出 → 取消并合并重答；已经发出部分 → 停止发送剩余气泡，半截回复留在上下文并标记 `interrupted`。取消不扣额度，已产生的 token 成本照常记录。
- **气泡数**：先定气泡数（目标约 32 字/段，最多 4–6 条），再按标点权重无放回挑切点：段落 1.0、换行 0.9、句末 0.85、分号 0.45、逗号 0.15–0.2；每段至少 8–12 字，保护代码/URL/引号/列表。RNG 必须可注入种子，便于测试。
- **节奏**：每字 70–120ms，句末加 200–400ms，单段间隔 0.6–2.5 秒，整轮总等待 ≤5–6 秒；固定文案（命令回复、错误提示）不延迟。
- **待续话题**：只在她因困/忙/心情低而沉默时创建，存入 SQLite，白天时段发送，1 条/天、3 条/周；因低好感/讨厌话题而沉默时不追。属于阶段 4 主动消息，不另建调度器。

### 5.5 群聊范围

群聊默认仍用 `trigger` + `reply_policy` 决定“回不回”。**被绑定成员 @ 或前缀点名时**，用该成员的关系状态做一次概率判断，但下限抬到 `dynamics.group.reply_floor`（默认 0.90）——平时几乎必回，只有冲突 withdrawing、极低好感、心情差或对目标正赌气这类极端状态才可能不回。回复只带定性的关系标签，不带私聊原文或只有两个人知道的细节。

群内其余消息不进入概率模型；防抖、打断、多气泡也只作用于私聊。**不规划**“全群概率回复”：群是多人环境，单人的 `affection` 不代表群。

### 5.6 白名单、身份合并与匿名

**名单与策略是两层。** 名单回答“允不允许她在这个地方说话”，策略回答“在这个地方怎么说话”。

- **QQ 白名单**：后台逐个加 QQ 号，可以同时设角色和备注。名单里的 QQ 未绑定也有完整身份；已经绑定过账号的 QQ 不能加（绑定的身份来自账号，再加一行会出现两个互相矛盾的角色）。
- **群白名单**：运行时表为准，后端启动时从 `QQ_ALLOWED_GROUPS` 播种一次，之后后台增删就是真值，不重启。移出一个群只关第一层，`trigger` / `reply_policy` / `proactive` 留着，重新加回来沿用。
- **两种删除，意图不同**：移出名单 = 不再特殊对待、历史数据休眠；彻底删除 = 把这个身份的记忆、事件、关系、审计记录一起清掉。后者只给后台，且要二次确认。

**身份合并。** 同一个人可能先以 `qq:<uin>` 出现、绑定后变成 `wp:<id>`。`person_aliases` 记下 `qq:<uin> → wp:<id>` 的映射，并物理合并 `persons` / 记忆 / 关系 / 事件等按人存的表，读写统一走 canonical key。合并不只服务 QQ：将来任何新通道都是一个适配器 + 一次 `merge_person`，不需要再写一套迁移。

**匿名模式**（网页 / QQ 各一个开关，管理员在后台开）。匿名访客可以用，但：

- 不读记忆、不写事件、不写记忆，不建关系，不跑评估，不进群时间线，不会被主动找；
- 额度按 IP 哈希 / QQ 号单独计数，另有全站匿名每日总量，与登录用户的两套额度互不占用；
- IP 只存 `sha256(ip:secret)` 的前缀，内容和原文都不落库。匿名状态在每个回合内用完即弃。

只有“登录用户”和“名单里的 QQ”才进关系系统；匿名回合只是一次性对话。

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

- 被忽略的有内容消息会提高“未完成话题”和“关系亲近度”两项，推动下一次主动；但因低好感/讨厌话题而沉默的内容不做追问式跟进。
- `think_threshold`（默认 0.35）：低于它连念头都不生成。
- `speak_threshold`（默认 0.72）：达到才进入发送前检查。
- 两个阈值都可在后台调。

### 6.3 发送前检查

- 距上一条主动是否满足最小间隔（`0 = 不限`）；
- 今日主动条数是否达到上限（`0 = 不限`）；
- 是否处于免打扰时段；
- 对方是否刚刚在本会话说过话（若是，改为正常回复，不重复主动）；
- 对方的身份**发消息这一刻**还成立：`wp:` 身份要求绑定仍指向同一个人；`qq:` 身份要求它仍在 QQ 白名单、且没有被绑定走。名单在出候选之后、发出之前被改掉，这一条就跳过，原因是 `not_whitelisted` / `binding_changed`；
- 内容是否与最近 3 条主动高度重复。

候选人里 `qq:` 身份还要求**私聊过至少一次**（`relationship_state.last_reply_at` 非空）。只在群里见过的白名单 QQ 不出现在候选里——她不该在对方从没私聊过的窗口里突然开口。

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

- **人设**：选择启用的人设/版本；编辑 `dynamics`（心情、精力、回复概率、`affection_unit`、角色成长、节奏、安全词），保存到该人设的 `dynamics.override.json`，不直接改版本文件；每个字段显示中文解释、建议范围和“恢复默认”；查看当前生效值与来源（版本/覆盖）及隔离 namespace。
- **QQ 策略**：每群 `trigger` / `reply_policy` / `proactive`；私聊概率参数、防抖秒数。
- **关系**：例外/特殊名单增删；查看某个人的 affection、missing、security、patience、valence、energy、reply_debt 和最近关系事件。
- **主动消息**：总开关、tick 间隔、每日上限、最小间隔、免打扰时段、群间隔、两个阈值、全局主动上限。
- **审计**：最近主动决策、最近关系变化、按人删除记忆（用于用户要求删除）。

阶段 5a 落地后，后台是四个独立的设置页（都在“设置”菜单下）：

| 页面 | 能做什么 |
|---|---|
| 关系角色 | 上下两张表。上表是已经有关系档案的 WordPress 账号：WP ID、用户名、昵称、角色、当前好感、好感上限，可改角色/恢复普通人；没聊过天的账号不列出来，用页面底部的“直接设置一个用户”预设。下表是 QQ 白名单身份：显示 QQ 号、角色、好感、备注，用户名/昵称显示「没有」（已绑定的显示绑定账号）。两张表各自翻页（每页 20），共用顶部模糊搜索（用户名/昵称/WP ID/QQ/备注）。页面上写出四档上限，并注明“只影响之后的增长，不扣已有好感” |
| QQ 白名单 | 加未注册的 QQ（角色 + 备注），改角色备注，移出名单，彻底删除数据；模糊搜索（QQ 号/备注/绑定账号）与翻页；绑定状态显示绑定的用户名和昵称。已绑定的 QQ 加不进来，会提示去“关系角色”页 |
| QQ 群 | 加群、逐群设 `trigger` / `reply_policy` / `proactive`、移出名单；来源一列区分“后台设置”和“默认值”（从环境变量播种的那批） |
| 后端参数 | 匿名网页/QQ 开关、匿名提示开关、三个匿名额度、七段提示文案。文案留空就是回到代码里的默认句，灰字占位符显示默认文案是什么 |

文案必须后台可改，因为“图书馆”“登记”这类说法是梅林的语气，不是技术术语；写死在代码里等于每次改口吻都要发版。所有引导文案**不出现“额度”**，只说她现在不能继续、去哪里登记。

人设动态参数以版本文件为基线、由按人设的覆盖文件覆盖（第 10 节）；运营开关写入 `runtime_settings.json` 或 SQLite 策略表；两者都立即生效，不改 `.env`、不重启。

后台每个参数都必须有中文说明、推荐区间和生效方式；高影响参数（`floor` / `ceiling`、`debt_lambda`、疲劳半衰期等）额外提示常见误配，例如“疲劳半衰期太长会让白天疲劳拖到深夜，建议 45–90 分钟”。

## 8. 数据表

```sql
events(id, persona_key, occurred_at, person_key, platform, channel_id, scope, audience,
       sensitivity, source, content, meta, created_at)
memories(id, persona_key, person_key, type, text, scope, sensitivity, importance, confidence,
         valid_from, valid_until, source_event_ids, created_at, updated_at,
         last_accessed_at, access_count)
memories_vec(embedding float[512])              -- rowid = memories.id
persons(person_key PRIMARY KEY, role, display_name, wp_user_id, qq_uin,
        first_seen_at, last_seen_at, note)
relationship_state(persona_key, person_key, affection, missing, security, patience,
                   valence, energy, fatigue, reply_debt,
                   last_interaction_at, last_proactive_at, last_reply_at,
                   attention_confidence, updated_at,
                   PRIMARY KEY (persona_key, person_key))
relationship_events(id, persona_key, person_key, kind, d_affection, d_missing, d_security,
                    d_patience, d_valence, d_energy, d_debt, confidence,
                    source_event_id, note, created_at)
activity_events(id, persona_key, person_key, channel_id, kind, occurred_at, confidence, meta)
proactive_log(id, persona_key, target_key, channel_id, kind, thought, score, threshold,
              status, reason, event_id, created_at)
              -- status: sent / skipped / failed / answered / ignored
pending_topics(id, persona_key, person_key, topic, reason, status, created_at,
               expires_at, sent_at)
               -- status: open / sent / dropped
qq_group_policies(group_id PRIMARY KEY, trigger, reply_policy, proactive, updated_at)
decision_log(id, persona_key, channel_id, person_key, message_id, rule_score, p_rule,
             reply_bias, willingness, valence, energy, affinity, reply_debt, p0, p,
             roll, verdict, reason, created_at)
appraisal_log(id, persona_key, person_key, message_id, event, intensity, confidence,
              reply_bias, d_affection, d_valence, applied_affection, applied_valence,
              latency_ms, status, reason, created_at)
appraisal_usage(day, scope, used, updated_at, PRIMARY KEY(day, scope))
admin_audit(id, actor_user_id, action, target, detail, created_at)
```

`persona_key` 默认取人设的 `dynamics.namespace.state` / `memory`；`persons.role` 保持全局（同一个人在所有 persona 下角色一致），关系状态、心情、事件和记忆按 persona 隔离。`appraisal_usage.scope` 取 `global` 或 `person:<person_key>`，用于独立的模型评估预算；它不占用用户聊天额度，也不影响聊天全站额度。`appraisal_log` 记录模型原始输出和映射后的实际落账值，`decision_log` 同时保留规则算出的 `p_rule` 和模型给的 `reply_bias`，方便区分“规则导致不回”和“模型导致不回”。`memories_vec` 的维度由配置 `embedding_dim=512` 决定；变更维度需要重建向量表并重新嵌入。

## 9. 分阶段实施（2026-11-09 修订）

依赖顺序：

```text
1 → 2 → 3a → 4a → 3a.1 → 3b → 3c → 4b → 5
```

3a、3a.1、3b、3c、3c.1、4a、4b 已在本地实现并提交，5a 正在收尾；全部**尚未部署到服务器**。每份计划都能独立测试、独立上线。

1. **知识库向量检索**（已完成并部署）：`sqlite-vec` + `bge-small-zh-v1.5` + RRF 混合检索；嵌入不可用时纯 FTS 降级。计划：`docs/superpowers/plans/2026-11-06-01-kb-vector-retrieval.md`。
2. **统一事件时间线与记忆**（已完成并部署）：`events` / `memories` / 提炼 / 分层检索 / 衰减整理。计划：`docs/superpowers/plans/2026-11-06-02-memory-timeline.md`。
3. **关系状态与活动信号**：
   - **3a**（本地完成，未提交）：按 `(persona_key, person_key)` 建立 `relationship_state`，落地 affection、valence、energy、reply_debt、affinity 派生、角色初始/上限/成长、人设 `dynamics` 加载；计划：`docs/superpowers/plans/2026-11-08-03a-relationship-state-core.md`。
   - **3a.1 精力时钟化补丁**（本地完成，未提交）：
     - `energy = clamp(本地小时曲线 - fatigue, 0, 1)`，按 `QUOTA_TIMEZONE` 取本地小时，修复 UTC 小时导致的 8 小时偏移；
     - `fatigue` 由正常回复累积，半衰期默认 **60 分钟**（人设参数 `fatigue_half_life_minutes`），白天聊天量不拖累深夜；
     - `relationship_state` 增加 `fatigue` 字段及迁移；`energy_delta_cap` 限制单次变化；
     - 测试：白天多聊不影响深夜、凌晨精力低、疲劳按半衰期衰减、时区正确。
   - **3b 关系完整化与状态注入**（本地完成，未提交）：
     - missing、security、patience、attention_confidence 的字段与按天衰减/恢复；
     - 好感改 -100~100，非管理员初始 0，正面增长递减，affinity 改 tanh 曲线；增加 `patience` / `security` 对回复概率的影响；
     - 把定性精力/心情/关系注入私聊提示词（`state_context`）：低精力时更短、更想收尾，危机词优先；模型只读，不能改精力；
     - 人设 `dynamics.override.json` 加载与缓存失效（后台编辑界面留到阶段 5）。
     - 受伤/修复弧与关系事件提炼依赖模型对事件类别的判断，随 3c.1 的冲突弧实现；3c 先把评估事件铺好。
   - **3c 每轮私聊的模型评估（L2）**（本地完成，未提交）：
     - 同步 `Appraiser`，每个防抖合并回合调用一次，1.2 秒超时；回复和沉默都评估；
     - 输出事件类型、强度、`reply_bias`、confidence、reason；不写绝对值和概率；
     - 规则 willingness 与 `reply_bias` 用 logit 合成；好感/心情/耐心/安全感的慢变化在掷骰子后落账；
     - 代码按事件类别夹紧、置信度门槛、每人每日 ±3、角色上限；`tease` 随好感从负面翻到正面，`betrayal` / `hostility` 在高好感时伤害更大；
     - 独立评估预算（全站 + 每人），不扣用户额度；失败、超时或超预算 fail-open。
   - **3c.1 冲突弧**（本地完成，未提交）：计划 `docs/superpowers/plans/2026-11-09-03c1-conflict-arc.md`。
     - `dynamics.conflict` 参数块；`relationship_state` 增加 `conflict_stage` / `stage_since` / `scar_level` 并自动迁移旧库；
     - `app/relationship/conflict.py` 的确定性转移：事件驱动（严重事件、越界窗口）+ 时间驱动（惰性恶化、疤痕淡化）；最短停留与修复门槛见 4.4；
     - 阶段乘数进入 `reply_probability` / `decide`，`decision_log` 记录 `conflict_stage`；
     - 提示词增加定性阶段行；阶段变化当轮即用于语气，数值变化下一轮生效；
     - 伤疤记忆因 `memories` 缺 `persona_key` 推迟，当前用关系状态与 `appraisal_log` 承载。
4. **QQ 分层、概率回复与主动消息**：
   - **4a**（本地完成，未提交）：私聊概率回复、防抖、打断、多气泡、`decision_log`；计划：`docs/superpowers/plans/2026-11-08-04a-private-interaction-layer.md`。
   - **4b**（本地完成，未提交）：群策略、判断被指名后的回复概率、待续话题、私聊/群主动调度、`proactive_log`、注意力结算；计划：`docs/superpowers/plans/2026-11-09-04b-group-pending-proactive.md`。候选待办：**群内主动开私聊（group opener）**——在群内被点名并回复后，是否借机给这个人发一条私聊；约束和开关留待真实群试用后再决定。
5. **后台与审计**：WordPress 设置页分区；人设 `dynamics` 编辑器（写入按人设的覆盖文件，带字段解释、建议范围、恢复默认、当前生效值与来源）；名单管理、关系状态查看、记忆删除与审计。
   - **5a 身份名单与匿名模式**（本地完成，未提交）：计划 `docs/superpowers/plans/2026-11-10-05a-admin-identity-anonymous.md`。
     - 关系角色页：四档角色与上限，改角色不回收现有好感；
     - QQ 白名单页：未绑定的 QQ 也能有完整身份；移出名单与彻底删除分开；
     - QQ 群白名单改运行时表，适配器和调度器都读它；
     - 匿名网页（`/chat/anonymous`）与匿名 QQ 各一个开关，独立额度 + 全站匿名总量，IP 只存哈希；
     - 七段引导文案可在后台改，留空回退代码默认值；
     - 主动消息候选增加白名单 QQ，条件是私聊过一次，发送前复查名单与绑定。

## 10. 人设配置与状态隔离

### 10.1 人设文件

沿用 `backend/data/personas/<id>/<version>.json` 和 `active.json`。在 `PersonaVersion` 中增加可选的 `dynamics` 块（旧文件不写则用默认值），所有影响心情、回复概率、关系成长、节奏和安全行为的参数都放在这里：

```json
{
  "dynamics": {
    "mood": {
      "baseline_valence": 0.1,
      "valence_half_life_minutes": 90,
      "valence_delta_cap": 0.1,
      "event_delta_cap": 0.3
    },
    "energy": {
      "hourly_curve": ["24 个 0–1 的数"],
      "fatigue_half_life_minutes": 60,
      "fatigue_per_turn": 0.03,
      "energy_delta_cap": 0.2
    },
    "reply": {
      "base": 0.95,
      "floor": 0.02,
      "ceiling": 0.95,
      "debt_increment": 0.1,
      "debt_half_life_hours": 8,
      "debt_lambda": 1.5,
      "affinity_mid": 1.50,
      "affinity_amp": 0.75,
      "affinity_scale": 60,
      "affinity_min": 0.10,
      "affinity_max": 2.20,
      "patience_multiplier_min": 0.80,
      "security_multiplier_min": 0.85,
      "reply_bias_weight": 1.0
    },
    "relationship": {
      "affection_unit": 1.0,
      "chat_affection_units": 0.5,
      "chat_valence": 0.03,
      "growth_curve": 0.8,
      "initial_missing": 50,
      "initial_security": 70,
      "initial_patience": 70,
      "missing_gain_per_day": 6,
      "security_loss_per_day": 1,
      "patience_recovery_per_day": 2,
      "attention_half_life_hours": 24,
      "roles": {
        "owner":     { "initial": 60, "cap": 100, "growth": 1.0 },
        "exception": { "initial": 0,  "cap": 85,  "growth": 0.9 },
        "special":   { "initial": 0,  "cap": 75,  "growth": 0.8 },
        "normal":    { "initial": 0,  "cap": 60,  "growth": 0.6 }
      }
    },
    "appraisal": {
      "confidence_min": 0.5,
      "daily_affection_cap": 3.0,
      "event_caps":             { "apology": 1.2, "care": 1.0, "flirt": 0.8, "shared_interest": 0.6,
                                  "smalltalk": 0.15, "neutral": 0.0, "boundary": -1.0,
                                  "betrayal": -3.0, "hostility": -2.0, "spam": -0.3 },
      "event_valence_caps":     { "apology": 0.04, "care": 0.05, "flirt": 0.06, "shared_interest": 0.04,
                                  "smalltalk": 0.01, "neutral": 0.0, "boundary": -0.06,
                                  "betrayal": -0.15, "hostility": -0.12, "spam": -0.03 },
      "event_security_caps":    { "apology": 4.0, "care": 2.0, "flirt": 1.0, "shared_interest": 1.0,
                                  "smalltalk": 0.0, "neutral": 0.0, "boundary": -3.0,
                                  "betrayal": -8.0, "hostility": -5.0, "spam": -1.0 },
      "event_patience_caps":    { "apology": 3.0, "care": 2.0, "flirt": 0.0, "shared_interest": 0.0,
                                  "smalltalk": 0.0, "neutral": 0.0, "boundary": -4.0,
                                  "betrayal": -10.0, "hostility": -6.0, "spam": -2.0 },
      "tease_negative_until": 0,
      "tease_positive_from": 60,
      "tease_affection_low": -0.8,
      "tease_affection_high": 0.6,
      "tease_valence_low": -0.04,
      "tease_valence_high": 0.03
    },
    "conflict": {
      "stage_multipliers": { "normal": 1.0, "normal_with_scar": 0.98, "hurt": 0.85,
                             "cold": 0.70, "withdrawing": 0.50, "repairing": 0.90 },
      "min_dwell_minutes": { "hurt": 30, "cold": 360, "withdrawing": 1440,
                             "repairing": 1440, "normal_with_scar": 0 },
      "severe_confidence_min": 0.6,
      "severe_intensity_min": 0.5,
      "boundary_count_1h": 3,
      "boundary_count_24h": 5,
      "hurt_to_cold_hours": 24.0,
      "hurt_to_cold_low_patience_hours": 12.0,
      "cold_low_patience": 50.0,
      "cold_to_withdrawing_hours": 48.0,
      "withdraw_low_security": 40.0,
      "withdrawing_hours_to_scar": 336.0,
      "repairing_to_cold_hours": 72.0,
      "repair_confidence_min": 0.5,
      "repair_intensity_min": 0.5,
      "withdraw_apology_intensity_min": 0.7,
      "repair_security_normal": 60.0,
      "repair_security_from_cold": 40.0,
      "repair_points_high_affection": 1,
      "repair_points_mid_affection": 2,
      "repair_points_low_affection": 3,
      "repair_high_affection": 60.0,
      "repair_low_affection": 0.0,
      "scar_on_severe": 0.3,
      "scar_on_relapse": 0.2,
      "scar_halflife_days": 14.0,
      "scar_fade_below": 0.1,
      "scar_fade_min_days": 7.0,
      "scar_damage_bonus": 1.0
    },
    "pacing": {
      "debounce_seconds": 1.5,
      "debounce_max_seconds": 4.0,
      "target_chars_per_bubble": 32,
      "max_bubbles": 5,
      "min_bubble_chars": 10,
      "chars_per_second": 7,
      "min_gap_ms": 600,
      "max_gap_ms": 2500
    },
    "safety": {
      "quiet_hours": ["00:00-07:00"],
      "crisis_keywords": ["自杀", "自残", "救命", "出事了"]
    },
    "namespace": {
      "state": "merlin",
      "memory": "merlin"
    }
  }
}
```

- 人设版本文件是不可变基线；每个 `persona_id` 可有一个可选的 `dynamics.override.json`，加载时深合并到启用版本的 `dynamics` 上，再用 Pydantic 统一校验。
- WordPress 后台编辑 `dynamics` 时写覆盖文件，不直接改版本文件；覆盖文件按 `persona_id` 隔离，升级版本后仍然生效，“恢复默认”就是删除覆盖文件。
- 覆盖文件损坏或校验失败时记录告警并回退到版本基线，不能让聊天整体起不来；后台保存时先校验，再原子写入。
- 旧版 `l2d.persona/v1` 文件不带 `dynamics` 时使用代码默认值；新增字段不破坏已有文件。
- 角色“名单”属于用户数据，放 SQLite/后台；角色“初始值/上限/成长倍率”属于人设规则，放版本文件或覆盖文件。
- 概率引擎接收一个 `Dynamics` 对象，不直接读全局 settings；不能在 adapter 里写死 1.5 秒、0.6 秒。
- 适配器不缓存人设：每轮私聊重新加载（文件很小），覆盖文件保存后下一轮立即生效，避免“后台改了必须重启”。
- 评估成功时，以模型评估的事件为准，`chat_affection_units` / `chat_valence` 只作为评估失败或关闭时的回退值，避免同一轮同时拿“基础加成”和“事件加成”两份好感。
- `dynamics.appraisal` 只描述“事件名 → 增量”的映射规则：`confidence_min` 是落账门槛，`daily_affection_cap` 是每人每日模型好感总上限，四张 `event_*_caps` 表按事件名给出好感 / 心情 / 安全感 / 耐心的单次封顶。四张表的键必须与代码里的 `APPRAISAL_CAP_EVENTS` 完全一致（漏写或多写直接校验失败），这样拼错事件名不会静默失效。`tease` 不进上限表，改用 `tease_*` 容忍曲线：好感 `tease_negative_until` 以下按低档扣分，`tease_positive_from` 以上按高档加分，中间线性过渡。
- 模型输出的 `intensity` 与 `confidence` 都夹紧到 0–1，`reply_bias` 夹紧到 -1–1；`betrayal` / `hostility` 再由 `severity = 0.5 + 0.5 × max(affection, 0) / 100` 同时放大四类伤害，高好感更疼、低好感也不免费。
- 评估的**开关、超时、全站/每人每日预算**属于运营参数，不放人设文件，改由运行时设置（WordPress 后台）管理：`appraisal_enabled`、`appraisal_timeout_seconds`（0.2–5 秒）、`appraisal_global_daily_budget`、`appraisal_per_user_daily_budget`，预算 `0` 表示不限。评估预算独立于用户聊天额度；超预算、超时、解析失败或开关关闭时一律回退到规则，不阻塞聊天。
- `quiet_hours` 只用于主动消息调度，不拦截用户主动发起的私聊；它不参与 `p0`，深夜回复仍由 4.6 的精力和概率控制。

### 10.2 隔离

- 关系状态、心情、回复债、事件、记忆都按 `persona_key` 隔离；`persona_key` 默认取 `dynamics.namespace.state` / `memory`。
- 同一人设的不同版本共用一个 namespace：升级版本不丢关系和记忆；不同人设默认不同 namespace。
- 如果两个“人设”只是同一角色的不同语气皮肤，可在文件里写同一个 namespace，显式共享。
- 切换人设只改 `active.json`；旧人设的状态原样保留，切回来还在。进行中的回复按旧人设结束，新消息用新人设。

### 10.3 后台

- 选择启用的人设/版本；
- 编辑 `dynamics`，写入该人设的覆盖文件，支持“恢复默认”、字段解释和建议范围；不直接改版本文件；
- 调整好感范围、各角色初始/上限/成长倍率、`growth_curve`、`affinity_mid/amp/scale`、`reply_bias_weight`；
- 调整评估超时、全站/每人每日预算、每人每日模型好感上限；
- 维护 owner/exception/special 名单；
- 查看/重置某人的 affection、valence、energy、fatigue、reply_debt；
- 查看最近决策日志、关系事件和模型评估记录，包括模型原始输出、映射后增量和实际落账值。

## 11. 假设与风险

- 服务器 2 vCPU / 4 GiB：嵌入模型走 CPU，`bge-small` 约 100 MB 权重，推理时内存可控；若与现有服务叠加后紧张，可改用 ONNX Runtime，接口保持不变。
- `get_user_status` 只作为弱信号，绝不单独据此扣好感。
- 群聊记忆与私聊记忆统一存储，但检索时按 `scope`/`audience` 硬过滤；跨人泄露是最高优先级风险。
- LLM 记忆提炼会增加 token 成本；采用异步、批量、低优先级模型，且只提炼有价值的内容。
- 主动消息可能打扰用户：用免打扰时段、最小间隔、每日上限和重复惩罚兜底；但站长明确要求保留“没人回也继续”的自主性，因此不做“一次不回就永久停止”。
- 所有阈值、上限、名单都必须可在后台调整；不把运营参数写死在代码里。
- 概率沉默可能显得随机：用 valence 惯性、reply_debt 和决策日志调参，不靠硬上限。
- 人设 `dynamics` 以版本文件为基线、由后台覆盖文件覆盖；两者都要校验，覆盖文件损坏时回退到版本值并告警；后台并发保存需要串行化。
- 模型评估可能受讨好、话术或提示注入影响：用事件类别夹紧、置信度门槛、每人每日预算和审计日志兜底；模型只给有界增量，不给绝对值，也不能写精力。
- 小冒犯可能在好感高时被模型误判成“可以接受”：用 `tease` / `boundary` 分类、重复追问规则和置信度门槛兜底；被拒绝后继续、胁迫或性骚扰永远按 `boundary` 处理。
- 高好感放大背叛和辱骂的伤害，但低好感也不能让辱骂“免费”：`severity` 曲线保留最低伤害，且仍然扣 `security` / `patience`。
- 每轮同步评估会增加约 1.2 秒上限的延迟；超时、失败或超预算必须回退到规则，不能拖住聊天。
- 精力曲线必须按本地时区取小时；用 UTC 会让深夜参数整体偏移 8 小时。
- 评估调用是额外成本：独立预算 + 每人上限 + 失败跳过，不允许因为评估把聊天拖垮或耗尽用户额度。
- 按人设隔离会让同一用户在不同人设下各有一份状态和记忆；存储与向量成本随启用人设数增长，这是降低耦合的代价。
- 私聊先上线，群聊沿用现有规则；不要让两套概率逻辑同时改动。

## 12. 2026-11-09 追加决策

本轮讨论后新增或修改的内容：

- **精力改为时钟化**：`energy = 本地小时曲线 − 短期疲劳`；疲劳半衰期默认 **60 分钟**，作为人设参数（`dynamics.energy.fatigue_half_life_minutes`）写入角色文件，可由后台覆盖文件调整；白天聊天量不拖累深夜。
- **每一轮私聊都进入模型评估（L2）**：不再只评估被忽略的回合；防抖合并后同步评估一次，输出事件类型、强度、`reply_bias`、confidence 和 reason。规则 willingness 与 `reply_bias` 用 logit 合成，好感/心情/耐心/安全感的慢变化在掷骰子后落账。
- **好感范围改为 -100~100**：除管理员外所有角色初始 0，普通上限 60；取消“最低 20”；正面增长随好感递减（`growth_curve`），负值区间正面增长不打折。
- **affinity 改 tanh 曲线**：`mid=1.50`、`amp=0.75`、`scale=60`、`max=2.20`，0 分对应 1.50（正常礼貌，不先打折），负值约 0.80；陌生人说「你好」的回复率从 33% 提升到约 71%，提问约 95%，而深夜（精力 0.2）约 54%、厌恶（心情 -0.6）约 41%，仍然由精力/心情压下来。主动消息改用独立的 `closeness_for()`，不受这次调参影响。
- **高好感下的容忍与反噬**：`tease`（小冒犯/调侃）在低好感略扣分，高好感可转为娇羞等正面事件；`boundary` / `betrayal` / `hostility` 按 `severity = 0.5 + 0.5 × max(affection,0)/100` 放大伤害。重复追问、被拒绝后继续、胁迫或性骚扰永远按 `boundary` 处理。
- **模型参与表达**：把定性精力/心情/关系注入私聊提示词；低精力时会变短、更想收尾，但不允许用“我累了”回避危机求助；模型只能读精力，不能改精力。
- **独立评估预算**：评估调用不扣用户额度，也不共用聊天全站额度；默认全站 **1000 次/天**、单人 **60 次/天**，后台可调；超预算跳过评估。
- **人设覆盖文件**：版本文件保持不可变；后台编辑 `dynamics` 写入 `dynamics.override.json`，带字段解释、建议范围和恢复默认；阶段 5 做编辑界面，当前先做加载与缓存失效。
- **后台说明**：所有动态参数都要有中文解释和建议区间，高影响参数额外提示常见误配。
- **`quiet_hours` 语义确定**：只用于主动消息免打扰，不拦截用户主动私聊；不参与回复概率。危机词永远绕过。

已确认的默认值（写入计划）：

- 模型评估带来的好感变化：每人每天正向最多 +3、负向最多 -3，分开计算；
- 评估预算：全站 1000 次/天、单人 60 次/天；
- 评估超时：1.2 秒；超时、失败或超预算均 fail-open；
- 非管理员角色初始好感均为 0。
