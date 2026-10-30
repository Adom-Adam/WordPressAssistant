# 注册防护、三级身份与额外额度 —— 设计

日期：2026-10-30
状态：待确认

## 1. 背景

2026-10-30 00:00 前后，`marlavalley13`（邮箱 `gb_sherrillmcnutt8550@falderewonek.site`）
注册后 11 分钟改密码，特征与批量注册的垃圾账号一致。当前站点：

- 注册完全开放，没有任何域名/频率检查；
- 后台只能看到 WordPress 用户列表，看不到「谁在用助手、今天还剩几次」；
- 额度只有两档：管理员不限量、其他人按 `DAILY_QUOTA` 一刀切；
- 额度每天清零，没有「攒下来」的存量概念。

## 2. 目标

1. **注册防护**：按域名黑名单 + 频率限制拦截批量注册，正常用户无感。
2. **用户总览**：后台一个页面看到所有注册用户的邮箱、注册时间、等级、
   今日已用/剩余、额外额度，并能搜索、改等级、发额度。
3. **三级身份**：`admin` / `member` / `user`。会员限额更高但仍限额。
4. **额外额度**：不随日期刷新、用完不补的存量额度；每日额度用尽后自动接着扣；
   由管理员发放或用户凭兑换码获得。
5. **兑换码**：一套码既能发额度，也能把用户升级为会员。

## 3. 非目标

- **不做** QQ 账号绑定本身。本设计只在数据结构里预留 `qq_uin` 字段并显示为「—」，
  绑定流程、绑定码、跨端记忆同步都留给后续单独设计（见 README「后续待办」）。
- **不做** 邮箱验证、注册审核、付费。这次选的是「黑名单 + 频率限制」这一档；
  后面要升级到邮箱验证时，`registration_errors` 这个钩子位置不变，只加逻辑。
- **不做** 会员的除额度外的任何特权（换模型、更长上下文、优先响应等）。
- **不做** 自动删除垃圾账号。删用户是破坏性操作，仍由你在 WordPress 用户列表里手动做。

## 4. 已确定的决策

| 决策点 | 选择 | 理由 |
| --- | --- | --- |
| 注册监管 | 域名黑名单 + 频率限制 | 不改注册流程；垃圾号根本进不来，比「进来了但用不了」更省事 |
| 会员授予 | 管理员标记 **和** 兑换码 | 两条路共用同一张 `user_profile.tier`，不产生两套真相 |
| 额外额度 | 管理员发放 + 兑换码 | 一个账本，两种入口 |
| 会员时长 | **一律带时长** | 兑换码的 `value` 就是天数；管理员手动标记也要填天数；到期自动退回普通用户 |
| 全站电闸 | **硬上限**：全站每日额度满了，谁都不能聊 | 每一非管理员请求都占一个全站单位，不管花的是每日还是额外额度；否则电闸就不是真实的费用上限 |
| 管理入口 | WordPress 后台新增页面 | 复用现有 `manage_options` 与 `admin_token()`，不用记第二个地址 |

## 5. 关键架构判断（会影响后面所有代码）

### 5.1 等级和额度存在后端，邮箱和注册信息存在 WordPress

**判断：`tier`、额外额度、QQ 绑定字段放在后端 SQLite；邮箱、昵称、注册时间、
注册 IP 全部由 WordPress 提供，后端一个字节都不存。**

原因：

- 后端改不了 WordPress 用户表。如果把 `tier` 存在 WP 用户元数据里，
  兑换码升级就必须让后端反过来去写 WordPress（要么开 WP REST 写权限，
  要么在插件里轮询一个「待生效升级」表）——两条路都比现在复杂得多。
- 反过来，把邮箱抄一份到 SQLite，就多了一份会过期的副本，还是隐私负担。
  邮箱只有 WordPress 知道就够了。

于是「用户总览」页做的事是：`WP_User_Query` 拿到用户 + 邮箱 + 注册时间，
再调后端 `GET /api/admin/users` 拿到 `user_id → {tier, credit, used, limit}`，
在 PHP 里合并成一张表。**后端不返回邮箱，页面上也没有把邮箱发给后端的路径。**

### 5.2 JWT 里不加 `tier`，后端每次查询

`sub` 已经是对的，等级是可变的（兑换码随时能升级）。如果写进 15 分钟有效期的
JWT，会出现「刚兑换完，额度还是旧的，要等 token 过期」这种难解释的现象。
每次聊天本来就要查额度库，顺手多查一行 `user_profile` 不会多一次连接。

### 5.3 每日额度优先，额外额度兜底

消耗顺序：**先扣当天额度（会刷新），扣不动再扣额外额度（不刷新）**。
反过来做会让用户觉得「我的存量被偷偷吃掉了」。

这件事必须在**一个事务**里完成，否则并发下会出现「两个请求都发现每日满了，
各自扣了额外额度」或者「每日刚好返回 1 个位置但同时也扣了额度」。

### 5.4 全站额度是硬上限，不区分资金来源

`global_usage` 统计**所有非管理员对话**，不管这一次是花每日额度还是额外额度。
它回答的问题是「今天这个后端最多被调用多少次」，而每次调用都要真金白银付 token，
所以资金来源不该影响计数。

代价是：全站额度满了，持有大量额外额度的人也不能聊。这是刻意的——
如果存量能绕过电闸，那么「今天最多花多少钱」就不再是一个可依赖的上限，
而你在发放额度时也没法预估累计成本。

## 6. 数据模型

全部在 `QUOTA_DB_PATH` 指向的同一个 SQLite 文件里（沿用现有 `data/quota.sqlite3`），
用 `CREATE TABLE IF NOT EXISTS` 建表，**不需要迁移脚本**。

```sql
-- 用户档案：等级 + 额外的预留字段
CREATE TABLE IF NOT EXISTS user_profile (
    user_id         INTEGER PRIMARY KEY,
    tier            TEXT NOT NULL DEFAULT 'user',  -- 'user' | 'member'
    tier_expires_at TEXT,                          -- member 必填；到期由读取时惰性降级
    qq_uin          TEXT,                          -- 预留：QQ 绑定，本设计不写入
    note            TEXT,                          -- 你给这个用户写的备注
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 存量额度余额（没有日期列，所以天然不刷新）
CREATE TABLE IF NOT EXISTS credit_balance (
    user_id    INTEGER PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TEXT NOT NULL
);

-- 额度流水：每一次增减都留痕，出问题能查
CREATE TABLE IF NOT EXISTS credit_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,               -- 正数发放，负数消耗
    reason     TEXT NOT NULL,                  -- 'admin_grant' | 'redeem' | 'chat'
    ref        TEXT,                           -- 兑换码 / message_id
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user
    ON credit_ledger(user_id, id);

-- 兑换码
CREATE TABLE IF NOT EXISTS redeem_code (
    code       TEXT PRIMARY KEY,               -- 明文，形如 M7KQ-3XPD-9F2A-B6TR
    kind       TEXT NOT NULL,                  -- 'credit' | 'member'
    value      INTEGER NOT NULL,               -- credit: 条数；member: 天数
    max_uses   INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,                           -- NULL 表示永久
    note       TEXT,
    created_at TEXT NOT NULL
);

-- 兑换记录，防重复兑换 + 审计
CREATE TABLE IF NOT EXISTS redeem_log (
    code      TEXT NOT NULL,
    user_id   INTEGER NOT NULL,
    used_at   TEXT NOT NULL,
    PRIMARY KEY (code, user_id)
);
```

设计取舍：

- **`credit_balance` 和 `credit_ledger` 分开**：余额查得频繁，流水只在审计时看。
  两个表在同一个事务里更新。
- **兑换码存明文**：单次使用、默认 30 天过期、面值就是几次聊天，
  泄露后果有限；存哈希就没法在后台随时把码贴给用户了。如果你更想存哈希，
  文档标注一下即可改。
- **`user_profile` 不存邮箱**（见 5.1），所以它是一张可以随便删的派生表——
  用户被 WordPress 删掉后，这里留下孤儿行也不影响任何功能，后台列表按 WP 用户为准。
- **到期时间存成 ISO 字符串而不是布尔或天数**：`tier_expires_at` 直接可比大小，
  读的时候顺手判断即可；存「剩余天数」则每天都要重算，还要额外一个字段记录基准时间。

## 7. 后端改动

### 7.1 额度核心（`app/quota.py`）

`reserve()` 签名增加一个参数，语义变成三段式：

```python
def reserve(
    self,
    user_id: int,
    limit: int,
    *,
    is_admin: bool = False,
    global_limit: int = 0,
    allow_credits: bool = True,   # 新增：显式关闭可以让某些调用不碰存量
    day: str | None = None,
) -> QuotaReservation | None:
```

事务内顺序：

```
BEGIN IMMEDIATE
  1. 尝试 daily_usage 原子 +1（WHERE used < limit）
     成功 → source='daily'
  2. 失败且 allow_credits：
     尝试 credit_balance 原子 -1（WHERE balance > 0）
     成功 → source='credit'，同时写一条 -1 的 credit_ledger
  3. 1 和 2 都失败 → rollback，返回 None（429 文案「今天的额度用完了」）
  4. 不论走到 1 还是 2：若 global_limit > 0，global_usage 原子 +1
     失败 → rollback（连带撤销刚占的每日额度或存量），抛 GlobalQuotaExceeded
  5. commit
```

第 4 步放在最后、和前面同一个事务里，是因为全站额度是**硬上限**（见 5.4）：
它必须挡住所有非管理员对话，也要保证「全站满了」时个人的每日计数和存量
都不会被白白扣掉。

- `QuotaReservation` 增加 `source: Literal["admin", "daily", "credit"]`。
- `refund()` 按 `source` 退回对应账户（credit 的退回同时写一条 `+1 chat_refund` 流水）；
  两种 source 都要退全站计数（第 4 步占过的那个单位）。
- `status()` 增加 `credit_balance` 与 `tier`，返回给前端。
- 新增方法：
  - `tier_of(user_id) -> tuple[str, str | None]`：返回 `(tier, expires_at)`。
    **惰性降级**——读到 `member` 且 `tier_expires_at <= now` 时，顺手把这一行
    改回 `user`、清空到期时间，再返回 `("user", None)`。不需要定时任务，
    也不需要「最后一天忘记跑脚本」的运维。
  - `grant_credits(user_id, delta, reason, ref=None) -> int`（返回新余额，delta 可为负）
  - `set_tier(user_id, tier, days: int | None) -> str | None`：
    `tier="member"` 时必须给 `days`，从「现在或当前到期日中较晚的那个」开始加，
    所以连续兑换/续期不会浪费尚未用完的时间；`tier="user"` 时清空到期时间。
  - `profile_snapshot(user_ids: list[int]) -> dict[int, dict]`（后台列表用，一次查询取齐）
  - `create_codes(kind, value, count, max_uses, expires_at, note) -> list[str]`
  - `redeem(user_id, code) -> RedeemResult`（内部事务：查码→校验次数/过期→
    写 `redeem_log`→额度或等级→`used_count+1`）

**全站额度与额外额度的关系**：额外额度消耗**同样计入**全站额度（见 5.4）。
所以 `global_counted` 在 `source="credit"` 时也为真，退款时要一起退。

### 7.2 配置（`app/config.py` + `app/runtime_settings.py`）

`.env` 与运行时设置新增：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `MEMBER_DAILY_QUOTA` | `60` | 会员每日额度 |
| `CREDIT_MAX_PER_USER` | `10000` | 单用户存量上限，防误操作发错位数 |

`DAILY_QUOTA`（普通用户）保持 20 不变。`runtime_settings.py` 里两个 key 加进
`_FIELD_LIMITS`，就能自动获得「后台改完立即生效」和「坏值逐字段回退」。

### 7.3 接口

| 方法 | 路径 | 鉴权 | 用途 |
| --- | --- | --- | --- |
| POST | `/api/redeem` | 用户 JWT | 用户自己兑换；`{code}` → `{kind, tier?, tier_expires_at?, credit?, message}` |
| GET | `/api/quota` | 用户 JWT | **扩展**返回值：`credit_balance`、`tier`、`tier_expires_at` |
| GET | `/api/admin/users` | 管理员 JWT | **全量**档案快照（不含邮箱），一并返回每日用量 |
| PATCH | `/api/admin/users/{user_id}` | 管理员 JWT | `{tier?, duration_days?, note?}`；`tier="member"` 时 `duration_days` 必填 |
| POST | `/api/admin/users/{user_id}/credits` | 管理员 JWT | `{delta, reason}`，正负均可 |
| POST | `/api/admin/codes` | 管理员 JWT | `{kind, value, count, max_uses, expires_at, note}` → 返回码列表 |
| GET | `/api/admin/codes` | 管理员 JWT | 最近 50 个码 + 使用情况 |

`/api/redeem` 的防爆破：同一用户 10 分钟内最多 5 次失败尝试（内存计数器，
和 QQ 适配器的去重一样存在进程内即可），超了返回 429。

### 7.4 聊天链路（`app/api/chat.py`）

只有一处变化：

```python
# tier_of 内部会做到期降级，所以这里拿到的永远是“此刻有效”的等级
tier, _expires_at = await asyncio.to_thread(store.tier_of, user.id)
limit = runtime.member_daily_quota if tier == "member" else runtime.daily_quota
reservation = await asyncio.to_thread(
    store.reserve, user.id, limit,
    is_admin=user.is_admin, global_limit=runtime.global_daily_quota,
)
```

`tier_of` 和 `reserve` 是两次独立的数据库访问。它们之间理论上有极小的窗口
（刚过期的那一瞬间），但结果只会多给一次会员额度或少给一次，
不影响任何安全性，所以不为它加事务。

429 的文案按 `source` 区分没有意义（都是「用完了」），但可以补一句
「会员每天有 N 条，额外额度用完后不会自动恢复」，放在详情里更省支持成本。

### 7.5 用户互不影响的保证

- 所有新表都以 `user_id` 为主键/索引，没有全表扫描路径；
- `profile_snapshot([])` 在用户为空时直接返回 `{}`，不会发 SQL；
- 后台页对每个请求做一次快照，不做 N+1 查询。

## 8. WordPress 侧

### 8.1 注册防护（`wp-live2d-ai.php`）

新增两个选项：

- `l2d_ai_blocked_email_domains`：一行一个域名，匹配时忽略大小写并包含子域
  （`falderewonek.site` 同时拦 `x@a.falderewonek.site`）。
  初始值写入 `falderewonek.site`。
- `l2d_ai_register_limit`：`{per_ip_hour: 3, per_ip_day: 10, min_interval_seconds: 30}`。

实现要点：

1. `registration_errors` 过滤器里做域名检查，命中就 `$errors->add(...)`，
   文案保持中立（「该邮箱域名暂不支持注册」），不提示"你被识别为机器人"。
2. 同一个过滤器里做频率限制：用 transient 计数，键为
   `l2d_ai_reg_<md5(ip)>_h` / `_d`；IP 通过 `$_SERVER['REMOTE_ADDR']` 取，
   如果站点在宝塔 Nginx 后面且未配置真实 IP 头，退化为「未知 IP 只做全天计数」。
3. `user_register` 钩子：把注册 IP 和注册时间写进用户元数据
   `l2d_ai_reg_ip` / `l2d_ai_reg_at`，供总览页显示。
4. 每次拦截写一条记录进 `l2d_ai_block_log` 选项（环形数组，保留最近 50 条：
   时间、邮箱、IP、原因）。这是调参的唯一依据，比日志文件好用。

### 8.2 新增后台页

两个 `add_options_page`（都在「设置」下，和现有设置页平级）：

**A. Live2D 小助手 · 用户与额度**（`l2d-ai-users`，`manage_options`）

- 顶部筛选：搜索框（昵称/邮箱）、等级筛选（全部/会员/普通）、分页（每页 30）。
- 表格列：ID / 昵称 / 邮箱 / 注册时间 / 注册 IP / 等级 / 会员到期 / 今日已用 /
  今日剩余 / 额外额度 / QQ（一直是「—」）/ 操作。
- 操作：切等级（下拉 + 天数输入；选「普通」时天数框忽略）、发额度
  （数字 + 原因，可负数）、备注。全部走 `admin_post_` 表单 + `_wpnonce`，
  成功后 `redirect_with_note`。
- 页面底部「兑换码」区块：生成表单（类型/面值/数量/可用次数/有效期/备注），
  生成后原地列出码；下面列出最近 50 个码及其已用次数。

**B. Live2D 小助手 · 注册防护**（`l2d-ai-guard`，`manage_options`）

- 域名黑名单文本域（一行一个）+ 保存；
- 频率限制三个数字 + 保存；
- 最近 50 条拦截记录表（时间 / 邮箱 / IP / 原因）；
- 一句提示：删用户请到「用户 → 所有用户」，本页不提供删除。

### 8.3 为什么不在插件里保存等级

见 5.1。插件只做「读后端快照 + 写后端接口」，自己不存 `tier`。
好处是兑换码升级和后台手动升级落在同一张表里，不会再出现两套状态不一致。

## 9. 前端

只加两处，都是小改动：

1. **额度条**（`ui/assistant.ts`）：当 `credit_balance > 0` 时显示
   「今日剩余 3 条 · 额外 12 条」，否则维持现状。
2. **兑换入口**：额度条旁边一个「兑换」链接（仅登录用户可见），点击弹出
   输入框，调 `POST /api/redeem`，成功后就地刷新额度与等级提示。

Spine/Live2D 形象层完全不动。

## 10. 安全与隐私清单

| 项 | 处理 |
| --- | --- |
| 邮箱泄露面 | 只存在 WordPress，后端接口不返回邮箱 |
| 兑换码爆破 | 16 位 Crockford Base32（`secrets` 生成），单用户 10 分钟 5 次失败上限 |
| 兑换码重放 | `PRIMARY KEY (code, user_id)` + `used_count` 原子 `+1 ... WHERE used_count < max_uses` |
| 后台越权 | 所有新页面 `manage_options` + `_wpnonce`；后端接口 `require_admin` |
| 额度为负 | `CHECK (balance >= 0)`；扣减用 `WHERE balance > 0` 原子条件 |
| 发错额度 | `CREDIT_MAX_PER_USER` 上限；流水表可追溯、可反向补偿 |
| 注册防护误伤 | 黑名单只匹配域名；限流按 IP 且阈值宽松（3 次/小时）；拦截记录可见可调 |

## 11. 验收标准

后端（`uv run pytest -q`，新增约 15 个用例）：

1. 每日额度有剩时只扣每日，不动额外额度；
2. 每日额度用尽、额外额度 > 0 时继续可用，返回 `used`/`balance` 正确；
3. 两者都空时返回 429；
4. 退款按 `source` 退回正确的账户，且全站计数一并退回；
5. 并发 40 个请求、每日剩 5、额外 10：恰好 15 个成功，余额为 0，无负数；
6. **全站额度是硬上限**：全站满时，持有额外额度的人也拿到 429，
   且失败请求没有扣掉任何每日额度或存量（事务回滚）；
7. 兑换码：正常、过期、次数用尽、同一用户重复兑换、不同用户共用一个多次码；
8. 会员额度生效：同一用户改成 `member` 后 `limit` 变化；
9. **会员到期**：把 `tier_expires_at` 设成过去时间后，下一次 `tier_of` 返回 `user`
   并把表里的值改回 `user`；续兑会员码从原到期日往后加，不从今天重算；
10. 非管理员访问 `/api/admin/*` 返回 403；
11. `tier="member"` 但没给 `duration_days` 时返回 422；
12. 管理员发放负额度不会把余额扣成负数。

手动验证（我会给你逐条命令/截图步骤）：

1. 用 `falderewonek.site` 注册被拒，页面提示中立文案；
2. 连续 4 次同 IP 注册，第 4 次被限流；
3. 后台总览页能看到用户列表、邮箱、注册 IP，改等级后聊天额度立刻变化；
4. 生成一个 5 条的兑换码，用普通账号兑换，额度条出现「额外 5 条」。

## 12. 已确认的判断

2026-10-30 讨论后确定，不再作为开放问题：

1. **会员一律带时长**。兑换码的 `value` 是天数；管理员手动标记也要填天数；
   到期由读取时惰性降级，不依赖定时任务；续期从原到期日往后加。
2. **全站额度是硬上限**。额外额度同样计入全站计数，电闸跳了谁都停。

仍按「先这样」处理、你想改随时说的一条：

- **兑换码存明文**（见第 6 节）。面值和用途都很低敏，图的是你随时能在后台复制；
  若要存哈希，后台就只能「生成时显示一次」，之后无法再查。改动只涉及
  `redeem_code` 表与生成/校验两处代码。

## 13. 实施顺序（确认后按此推进）

1. 后端：建表 + `reserve` 改造 + 单测（先把额度语义钉死，后面都依赖它）；
2. 后端：`/api/admin/users`、`/api/admin/codes`、`/api/redeem`；
3. 插件：注册防护 + 两个后台页；
4. 前端：额度条与兑换入口；
5. 文档：`docs/architecture.md`、`docs/tutorial.md`、`backend/README.md`、
   `docs/deployment-walkthrough.md` 各补一节（最后一次性写，不逐轮改文档）。
