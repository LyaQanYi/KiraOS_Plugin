# KiraOS 插件文档

> **插件 ID**: `kira_plugin_kiraos`  
> **版本**: 2.0.0  
> **作者**: LyaQanYi  
> **兼容**: KiraAI v2.0 (dev branch)

KiraOS 是 Kira 的 OS 级插件，整合了两大核心能力：

| 能力 | 类比 | 说明 |
|------|------|------|
| **用户记忆 (User Memory)** | RAM | 基于 SQLite 的按用户持久化画像与事件日志，自动注入 LLM 上下文 |
| **技能路由 (Skill Router)** | 程序加载器 | 渐进式工具发现——启动时加载轻量 manifest，运行时按需注入完整指令 |

---

## 目录

- [安装](#安装)
- [快速开始](#快速开始)
- [与内置记忆插件的关系](#与内置记忆插件的关系)
- [用户记忆系统](#用户记忆系统)
  - [工作原理](#记忆---工作原理)
  - [memory_update 工具](#memory_update-工具)
  - [操作类型详解](#操作类型详解)
  - [memory_query 工具](#memory_query-工具)
  - [consolidate_memory 工具](#consolidate_memory-工具)
  - [memory_clear 工具](#memory_clear-工具)
  - [主动记忆 (Active Recall)](#主动记忆-active-recall)
  - [上下文自动注入](#上下文自动注入)
- [Memory WebUI](#memory-webui)
  - [启用 WebUI](#启用-webui)
  - [REST API](#rest-api)
  - [认证](#认证)
- [技能路由系统](#技能路由系统)
  - [工作原理](#技能---工作原理)
  - [创建自定义技能](#创建自定义技能)
  - [SKILL.md 规范（格式 A）](#skillmd-规范格式-a)
  - [manifest.json 规范（格式 B，传统）](#manifestjson-规范格式-b传统)
  - [instruction.md 规范（格式 B 专用）](#instructionmd-规范格式-b-专用)
  - [第三层渐进披露：资源文件](#第三层渐进披露资源文件)
  - [完整示例：塔罗牌占卜](#完整示例塔罗牌占卜)
  - [示例技能](#示例技能)
- [配置项](#配置项)
- [数据存储](#数据存储)
- [架构概览](#架构概览)
- [故障排除](#故障排除)
- [更新日志](#更新日志)

---

## 安装

将整个 `KiraOS_Plugin` 文件夹放入 Kira 的内置插件目录即可：

```text
core/plugin/builtin_plugins/
└── KiraOS_Plugin/
    ├── __init__.py
    ├── main.py                ← 插件入口：工具注册 + 钩子 + 审计员调度
    ├── db.py                  ← SQLite 封装（含自动迁移、export/import、search）
    ├── skill_router.py        ← 技能发现 + 占位符替换 + 资源沙箱
    ├── web_server.py          ← Memory WebUI（REST API + 鉴权中间件）
    ├── manifest.json          ← 插件元数据（version 在此）
    ├── schema.json            ← 配置项 schema
    ├── README.md              ← 本文（含完整更新日志）
    ├── LICENSE
    ├── docs/
    │   └── active-memory-proposals.md   ← A+B+C 三方案讨论稿
    ├── skills/                ← 仓库自带的示例技能（首次需复制到 data/skills/）
    └── web/
        └── index.html         ← 单文件 SPA 前端
```

无需修改任何代码。重启 Kira 后插件会自动被发现并加载。可在 WebUI 的插件管理页面中启用/禁用及调整配置。

**两个有效的插件安装路径**：

| 路径 | 适用场景 |
|---|---|
| `core/plugin/builtin_plugins/KiraOS_Plugin/` | 把插件作为 Kira 仓库的一部分发布（built-in），需要 push 到代码库 |
| `data/plugins/KiraOS_Plugin/` | 用户级安装，插件目录与代码库分离，便于本地试用或非合并维护 |

二选一即可，**不要同时放两份**——会出现重复的 plugin_id 警告。Kira 启动时会先扫 `builtin_plugins/`，再扫 `data/plugins/`。

> ⚠️ **不要**将插件放在 `core/plugin/` **根目录**下——Kira 的插件发现机制不会扫描该目录。

---

## 快速开始

安装后启动 Kira，插件会自动：

1. **检测并禁用内置记忆插件**（Simple Memory），避免工具名和 Hook 冲突
2. 初始化 SQLite 记忆数据库 (`data/memory/kiraos.db`)，旧版数据自动迁移（如 `expires_at` ISO 字符串 → unix epoch、新增 `event_logs.tag` 列）
3. 扫描 `data/skills/` 目录，发现并注册所有技能工具；若任一技能带资源子目录，额外注册 `read_skill_resource` 工具
4. 每次 LLM 调用前自动注入：用户记忆上下文（按分类过滤）+ 主动记忆触发提示（A+B 层）+ 技能列表
5. 若 `memory_auditor_enabled=true`，每轮主 LLM 完成响应后异步触发审计员补抓漏记的事实（C 层）

```text
data/
├── memory/
│   └── kiraos.db          ← 自动创建
└── skills/
    ├── tarot_reading/     ← 塔罗牌占卜
    ├── daily_fortune/     ← 每日星座运势
    ├── story_continue/    ← 接龙续写故事
    ├── emoji_interpret/   ← Emoji 解读
    ├── nickname_generator/← 昵称生成器
    └── personality_test/  ← 趣味性格测试
```

> **提示**: 仓库根目录下的 `skills/` 文件夹包含示例技能，首次使用时需将其复制到 `data/skills/` 目录下。

---

## 与内置记忆插件的关系

KiraOS 的记忆系统与 Kira 内置的 **Simple Memory** 插件（`kira_plugin_simple_memory`）存在功能冲突：

| 冲突项 | Simple Memory | KiraOS Memory |
|--------|---------------|---------------|
| LLM Hook | 向 `name="memory"` 注入上下文 | 向 `name="memory"` 注入上下文 |
| 工具名 | `memory_update`（按索引修改文本行） | `memory_update`（按用户批量操作画像/事件） |
| 存储 | `data/memory/core.txt`（纯文本） | `data/memory/kiraos.db`（SQLite） |

**自动互斥处理**：KiraOS 在 `initialize()` 时会自动检测 Simple Memory 插件状态。若 Simple Memory 已启用，KiraOS 会**自动将其禁用**并记录日志警告。此操作会：

- 注销 Simple Memory 的所有工具和 Hook
- 将禁用状态持久化到 `config/plugins.json`

如需切换回 Simple Memory，只需在 WebUI 中禁用 KiraOS 并重新启用 Simple Memory 即可。

---

## 用户记忆系统

### 记忆 - 工作原理

记忆系统分为两层存储：

- **用户画像 (Profile)**：长期键值对，如 `昵称=小明`、`城市=北京`。每条带 `confidence`（0-1）、`category`（basic/preference/social/other）、可选 `expires_at`（TTL）。值长度上限 500 字符。
- **事件日志 (Event)**：按时间排序的短期事件记录，如 `完成半马`、`通过面试`。可选 `tag`（如 `milestone`/`daily`/`mood`）。summary 上限 1000 字符，按用户保留最近 N 条（缺省 100）。

记忆触发由三层机制保证 LLM **主动**记录而不是被动等待——v2.0.0 起 `memory_update` 的 description 改为清单式正向触发（"宁记错不漏过"），每轮 system prompt 末尾追加触发提示，可选启用独立的审计员 LLM 兜底捕获漏记。详见下方 [主动记忆 (Active Recall)](#主动记忆-active-recall) 章节。

只有完全无事实信息的纯客套（"你好"/"哈哈"/"好的"/"谢谢"）以及命中黑名单关键词的消息（"别记"/"开玩笑"等）才不会被记录。

### memory_update 工具

这是 LLM 用于管理用户记忆的主要工具，支持批量操作。

**工具签名**：

```json
{
  "name": "memory_update",
  "parameters": {
    "operations": [
      {
        "op": "set | event | del",
        "key": "画像键名（set/del 时必填）",
        "value": "画像值（set 时）或事件描述（event 时）",
        "category": "basic | preference | social | other（可选，默认 basic）",
        "confidence": "0-1 的置信度（set 时**必填**，不传将拒绝）",
        "tag": "事件标签（event 时可选，如 milestone/daily/mood）",
        "ttl": "过期时间，如 30d、7d、12h、30m（可选）",
        "force": "强制覆盖更高置信度的现值（set 时可选）",
        "user_id": "目标用户ID（群聊场景可选；缺省=最后发言者；必须是当前对话中的某个发言者）"
      }
    ]
  }
}
```

**Batch 内规则**：
- 同一 `(op, key, user_id)` 在一次 batch 内会被去重，**最后一条胜出**（防 LLM 反复改主意）。`event` 操作不去重。
- `set` 必须显式带 `confidence`；不传会被拒绝并提示。这是为了让 LLM 主动判断"这条信息我有多确定"，避免无脑用默认 0.5。
- 每条 `set` 都会做**冲突预检**：若现值置信度比新值高 > 0.2 且值不同，**不写入**并返回 `conflict <key>: 现值'X'(0.9) 高于新值置信度...`。要强制覆盖：`force: true` 或把 `confidence` 提高到现值的 -0.2 以内。
- `value` 长度硬上限 500 字符，超出自动截断并标记 `(value truncated from N chars)`。事件 summary 上限 1000 字符。
- 群聊场景可在每条 op 里带 `user_id` 指向当前对话中的某位发言者；非发言者会被拒绝（防止跨用户写入）。

### 操作类型详解

#### `set` — 设置/更新用户画像

新增或更新一条画像键值对。若 key 已存在则更新值，若 key 为新增则检查是否达到上限。

```json
{"op": "set", "key": "昵称", "value": "小明", "category": "basic", "confidence": 0.9}
{"op": "set", "key": "城市", "value": "北京", "category": "basic", "confidence": 0.8}
{"op": "set", "key": "最近在减肥", "value": "是", "category": "other", "confidence": 0.5, "ttl": "30d"}
{"op": "set", "key": "正在准备考试", "value": "是", "category": "other", "confidence": 0.6, "ttl": "30m"}
```

#### `event` — 记录事件

追加一条事件日志。超出保留上限后自动删除最旧的记录。可加 `tag` 给事件分类（注入上下文时会显示为 `2026-04-01#milestone ...`）。

```json
{"op": "event", "value": "完成了第一次半程马拉松", "tag": "milestone"}
{"op": "event", "value": "今天吃了寿司", "tag": "daily"}
```

#### `del` — 删除画像

删除指定 key 的画像条目。

```json
{"op": "del", "key": "旧昵称"}
```

#### 批量操作示例

用户说：「我叫小明，在北京，今天跑完半马」

LLM 调用：

```json
{
  "operations": [
    {"op": "set", "key": "昵称", "value": "小明", "category": "basic", "confidence": 0.9},
    {"op": "set", "key": "城市", "value": "北京", "category": "basic", "confidence": 0.9},
    {"op": "event", "value": "完成半马", "tag": "milestone"}
  ]
}
```

返回：`已完成 3 项记忆操作: set 昵称=小明 [basic]; set 城市=北京 [basic]; event #milestone: 完成半马`
并附带当前画像摘要，帮助 LLM 检测矛盾信息。

**冲突场景示例**：用户已有 `城市=上海(0.9)`，LLM 想改为 `城市=北京(0.5)`：

```json
{"op": "set", "key": "城市", "value": "北京", "category": "basic", "confidence": 0.5}
```

返回：`conflict 城市: 现值'上海' (置信度 0.90) 高于新值置信度 0.50。如确认要覆盖，请提高 confidence 至 ≥0.70 或在操作上加 force=true。`

LLM 收到这个 hint 后可以：
- 提高 confidence 重试（如果真的更确定）
- 加 `"force": true` 强制覆盖
- 接受现值，本次不写

### memory_query 工具

用户主动查询自身记忆，或 LLM 在 system prompt 里只看到 `basic` 类时需要其他类信息时调用。返回按分类分组的画像和近期事件。

**参数**：

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `category` | string | 否 | 仅查指定分类（`basic`/`preference`/`social`/`other`），不传则返回全部 |
| `user_id` | string | 否 | 群聊场景查指定发言者（必须是当前对话中的某个用户）；不传则查最后发言者 |

**触发示例**：

- 用户问「你记得我什么？」「我的画像」 → `memory_query()` 全量
- 主 LLM 想确认用户 preference 但 system prompt 只注入了 basic → `memory_query(category="preference")`
- 群聊里想看小张的记忆 → `memory_query(user_id="zhang_san")`

### consolidate_memory 工具

记忆压缩 / 反思工具。当某用户的事件日志累积较多（≥ 20 条）或用户主动说"帮我整理记忆/总结一下我的事件"时调用。返回最近 N 条事件（默认 30，上限 100）+ 当前长期画像 + 一段反思任务指令；LLM 在同一轮 tool-loop 内识别出现 ≥3 次的稳定模式（如运动习惯、兴趣、作息），通过 `memory_update` 提升为长期 profile 条目。

**触发示例**：「帮我整理一下记忆」「总结下我最近的事件」；或者你也可以让 LLM 在每隔 N 轮自动 reflective 调用一次。

**返回内容结构**（节选）：

```text
<consolidation user_id="...">
## 最近 30 条事件
- 2026-04-01 [#daily] 跑了 5km
- 2026-04-03 [#daily] 跑了 3km
- ...
## 当前长期画像（避免重复创建）
- [basic] name = ...
- ...
## 你的任务
1. 识别出现 ≥3 次的稳定模式
2. 跳过已有的画像
3. 用 memory_update 创建对应 profile (按出现频率分配 confidence)
4. 简短中文总结你提升了哪些条目
</consolidation>
```

### memory_clear 工具

清除用户全部记忆数据（画像 + 事件日志）。仅当用户明确要求时调用。

**触发示例**：「忘记我」「清除我的记忆」「删除我的所有信息」

### 主动记忆 (Active Recall)

KiraOS 的记忆触发由三层机制保证 LLM 不会"漏记"：

#### 第一层：工具描述（A）

`memory_update` 的 description 写成**清单式正向触发**——LLM 看到用户消息里出现身份/地点/职业/关系/偏好/经历这些类别的词，就直接记。判断标准是"这条信息明天再聊还希望你记得就记"，**宁记错不漏过**（低置信度后续可被高置信度覆盖）。

只有完全无事实信息的纯客套（"你好"/"哈哈"/"好的"/"谢谢"）才跳过。

#### 第二层：每轮 hint 注入（B）

`inject_context` 钩子每轮都会在 system prompt 末尾加一段：

```text
📝 本轮检查: 用户若提到任何自身事实信息(姓名/地点/职业/关系/偏好/经历),
   主动调用 memory_update 记录。宁记错不漏过——低置信度记下来，后续会被高置信度覆盖。
```

这段以独立 `Prompt(name="memory_hint")` 的形式注入，不污染其他 system prompt 段。

#### 第三层：审计员（C，可选）

每轮**主 LLM 完成响应后**，由一个独立的快速 LLM（默认走 `ctx.get_default_fast_llm_client()`）扫一遍用户消息，提取主 LLM 漏掉的事实。审计员是**异步、fire-and-forget**——不阻塞主对话流。

**默认关闭**，需在配置里启用：

```json
{
  "memory_auditor_enabled": true,
  "memory_auditor_model_uuid": "",  // 空 = 用 fast LLM
  "memory_auditor_skip_keywords": ["别记", "忘了它", "随便说说", "随便聊", "开玩笑", "假设说", "假如", "假设"]
}
```

**特性**：

- **去重**：每个 `KiraMessageBatchEvent` 只审一次（即使多步 tool loop 触发多次 step_result）
- **跳过关键词**：用户消息命中黑名单立即跳过，不发 LLM 请求（隐私 + 省成本）
- **置信度封顶 0.7**：审计员的写入永远不会盖过主 LLM 的高置信度判断（M6 冲突预检兜底）
- **JSON 输出**：审计员输出 JSON 数组直接落库，不走 LLM tool calling（更稳，对小模型更友好）
- **失败隔离**：解析失败 / API 超时 / DB 异常都只 log 不影响主流程
- **超时保护**：单次审计调用超时 15s 自动放弃

**审计员看到的上下文**：

1. 用户最新消息（命中黑名单则整个跳过）
2. 助手刚才的回复（语境）
3. 该用户当前的全部 profile（防止重复提取已有信息）

**与主 LLM 的协同**：审计员是**兜底**，主 LLM 仍然通过 `memory_update` 工具主动记。两边可能写同一 (user_id, key) — M1 batch dedup（同 batch 内）和 M6 冲突预检（跨调用）会自动处理冲突。

**成本估算**（Haiku 4.5、上下文 ~600 token in / 100 token out）：约 ¥0.007 / 轮。100 轮/天 ≈ ¥21/月。

### 上下文自动注入

每次 LLM 调用前，插件通过 `@on.llm_request` 钩子向 system prompt 注入两段独立内容：

**1. 记忆数据段**（`name="memory"`）—— 按分类分组并标注置信度：

```text
[user_123:basic] 昵称=小明(✓) | 城市=北京(✓)
[user_123:hint] 其他记忆类别可用: preference, social (调用 memory_query(category=...) 查询)
[user_123:events] 2026-04-01#milestone 完成半马 | 2026-03-28 通过面试
```

**置信度标记**：`✓` = 高（≥0.8）、`?` = 中（≥0.5）、`~` = 低（<0.5）

按 `inject_categories` 配置（缺省 `["basic"]`）只注入指定分类；未注入的分类自动追加 `:hint` 行提示 LLM 用 `memory_query(category=...)` 按需拉取。设为 `["*"]` 或 `["all"]` 恢复全量注入。当总字符数超过 `max_context_chars` 限制时，按分类优先级（basic > preference > social > other > events）截断。

事件行格式：`<日期>#<tag> <summary>`（无 tag 时不带 `#xxx`）。

**2. 主动记忆 hint 段**（`name="memory_hint"`）—— 每轮固定追加，让 LLM 把"是否要记"作为每轮的待办：

```text
📝 本轮检查: 用户若提到任何自身事实信息(姓名/地点/职业/关系/偏好/经历),
   主动调用 memory_update 记录。宁记错不漏过——低置信度记下来，后续会被高置信度覆盖。
```

> **注入目标**：插件先找 system prompt 里 `name="memory"` 的段，找到就追加；找不到就**新建**一个 `name="memory"` 段（v2.0.0 起的行为；v1.x 是追加到最后一段，会污染 `tools` 段）。`memory_hint` 段始终独立追加。

---

## Memory WebUI

KiraOS 内置了一个可视化记忆管理界面，基于 Starlette + 单文件 SPA 实现，可直接在浏览器中查看和管理所有用户的记忆数据。

### 启用 WebUI

在插件配置中设置 `webui_port` 为非零端口即可启用：

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `webui_port` | integer | 0 | WebUI 监听端口（0 = 禁用）。推荐值：`8765` |
| `webui_host` | string | `127.0.0.1` | 绑定地址。`127.0.0.1` 仅本机访问，`0.0.0.0` 允许远程 |
| `webui_token` | string | `""` | 访问令牌（留空则无认证，仅限本地使用安全） |

启用后，插件初始化时会自动启动 Web 服务器，终止时自动停止。

访问地址：`http://<webui_host>:<webui_port>/`

### REST API

WebUI 通过以下 REST API 与后端交互：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/stats` | 获取统计数据（用户数、画像数、事件数） |
| `GET` | `/api/users` | 列出所有用户（含画像/事件计数）。可选 `?q=<term>` 触发**跨表搜索**（user_id + 画像值 + 事件 summary），返回结果会带 `match_in: ["user_id"\|"profile"\|"event"]` 和 `snippet` |
| `GET` | `/api/users/{user_id}` | 获取用户的全部画像和事件 |
| `PUT` | `/api/users/{user_id}/profiles/{key}` | 新增/更新画像条目 |
| `DELETE` | `/api/users/{user_id}/profiles/{key}` | 删除画像条目 |
| `POST` | `/api/users/{user_id}/events` | 添加事件记录（可带 `tag` 字段） |
| `PUT` | `/api/users/{user_id}/events/{event_id}` | 更新事件内容（含 `tag`，传 `null` 清空 tag） |
| `DELETE` | `/api/users/{user_id}/events/{event_id}` | 删除单条事件 |
| `DELETE` | `/api/users/{user_id}` | 清除用户全部记忆 |
| `GET` | `/api/export` | 导出全部数据为 JSON 快照（自动设置下载头） |
| `POST` | `/api/import?mode=merge\|upsert\|replace` | 从快照恢复。`merge`=只补缺失、`upsert`=覆盖同 key、`replace`=先清空再导入。返回 `profiles_added/profiles_skipped/events_added` |

### 认证

设置 `webui_token` 后，所有 `/api/*` 请求**必须**携带 Bearer Token 头：

```text
Authorization: Bearer <your_token>
```

token 比较走 `secrets.compare_digest`（防时序攻击）。

**SPA 引导流程**：

- `/`（SPA shell）始终不需要认证（HTML+JS 是静态内容，没有数据）
- 用户首次访问 `http://host:port/?token=xxx`，前端 JS 把 token 读入 sessionStorage，**立刻从 URL 中移除** `?token=`，后续所有 API 调用走 Bearer 头

**v2.0.0 起 `?token=<token>` 查询参数对 `/api/*` 不再有效**——避免 token 泄露到 server log / referer / 浏览器历史。直接 `curl` 调用 API 的脚本必须改为 `Authorization: Bearer ...` 头。

> ⚠️ 若在公网暴露 WebUI，**强烈建议**设置 `webui_token` 并使用 HTTPS 反向代理。

---

## 技能路由系统

### 技能 - 工作原理

技能路由采用**渐进式披露 (Progressive Disclosure)** 模式，灵感来自 Claude 的 Skill 系统：

```text
┌─ 启动阶段 ─────────────────────────────────────────┐
│  扫描 data/skills/ 目录                              │
│  优先识别 SKILL.md（YAML frontmatter + 正文）        │
│  缺失则回退 manifest.json + instruction.md           │
│  仅加载轻量元数据，注册为 LLM 可用工具                │
│  若任一技能带资源子目录，额外注册 read_skill_resource │
└─────────────────────────────────────────────────────┘
              │
              ▼ LLM 决定调用某个技能
┌─ 运行阶段 ─────────────────────────────────────────┐
│  按需加载完整指令（SKILL.md 正文 / instruction.md）  │
│  替换参数占位符（格式 A: {{name}}, 格式 B: {name}）  │
│  XML 转义 + <user_input> 包裹（防提示注入）          │
│  附加用户记忆上下文 + 资源文件清单（如有）            │
│  作为 tool_result 返回给主 LLM                       │
│  主 LLM 在同一轮读取指令并直接执行                    │
│  零额外 API 调用                                     │
└─────────────────────────────────────────────────────┘
              │
              ▼ LLM 在执行中需要更多细节
┌─ 第三层（可选）资源按需加载 ────────────────────────┐
│  read_skill_resource(skill_name, path)               │
│  从 references/ resources/ scripts/ data/ 中读取     │
│  路径沙箱：禁止 .. 穿越，单文件 ≤ 200KB              │
└─────────────────────────────────────────────────────┘
```

**核心优势**：指令作为 tool_result 返回，主 LLM 在同一个 tool-loop 轮次中读取并执行，无需额外的 API 调用。复杂技能的额外资源只在主 LLM 主动需要时才会加载。

### 创建自定义技能

技能支持两种文件格式，二选一：

**格式 A（推荐，单文件，贴近 Claude Skills）**：

```text
data/skills/
└── my_skill/
    └── SKILL.md          ← YAML frontmatter + 执行指令正文
    └── references/       ← (可选) 第三层渐进披露资源
        └── deep_rules.md
```

**格式 B（传统两文件，向后兼容）**：

```text
data/skills/
└── my_skill/
    ├── manifest.json     ← 工具定义（必需）
    └── instruction.md    ← 执行指令（必需）
```

发现器优先识别 `SKILL.md`；如果缺失则回退到 `manifest.json + instruction.md`。两种格式可在同一个 `data/skills/` 目录下混用。

> 以 `_` 或 `.` 开头的目录会被跳过。

### SKILL.md 规范（格式 A）

文件以 YAML frontmatter 开头（两个 `---` 之间），其后是 Markdown 正文（即原来 `instruction.md` 的内容）。占位符使用 **双花括号** `{{param_name}}`（Jinja 风格），避免与正文里的 JSON 例子等单花括号语法冲突。

```markdown
---
name: tarot_reading
description: 当用户明确要求进行塔罗牌占卜、算命、或者预测运势时调用此工具。
exclude: 用户只是随口提到占卜但没有要求执行；用户问星座运势应走 daily_fortune。
command: /tarot
parameters:
  type: object
  properties:
    question:
      type: string
      description: 用户想要占卜的问题
  required: [question]
---

# 塔罗牌占卜技能

针对用户的问题「{{question}}」给出塔罗牌占卜结果……
```

字段含义与下方 `manifest.json` 完全一致；只是合并到了一个文件里。

### manifest.json 规范（格式 B，传统）

定义技能的元数据和参数。此文件必须为 JSON 对象。

```json
{
  "name": "技能名称（全局唯一，用作工具名）",
  "description": "技能简短标题",
  "trigger": "触发条件描述（帮助 LLM 判断何时调用）",
  "exclude": "排除条件（帮助 LLM 判断何时不调用）",
  "command": "/slash_command",
  "parameters": {
    "type": "object",
    "properties": {
      "param_name": {
        "type": "string",
        "description": "参数说明"
      }
    },
    "required": ["param_name"]
  }
}
```

**字段说明**：

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 技能名称，全局唯一。重复名称会被跳过并记录警告 |
| `description` | string | 否 | 技能的简短标题/说明 |
| `trigger` | string | 否 | 精确触发条件描述。注册工具时优先使用此字段，缺省回退到 `description` |
| `exclude` | string | 否 | 排除条件。执行时自动在指令前插入守卫提示，减少误触发 |
| `command` | string | 否 | 斜杠命令（如 `/tarot`），需启用 `enable_slash_commands` 才生效 |
| `parameters` | object | 否 | JSON Schema 格式的参数定义，缺省为空参数对象 |

### instruction.md 规范（格式 B 专用）

技能的完整执行指令。占位符使用 **单花括号** `{参数名}`（仅格式 B 使用，向后兼容；格式 A 请用 `{{参数名}}`）。运行时会被替换为实际参数值。

```markdown
# 技能标题

## 执行步骤
1. 根据参数 {param_name} 执行...
2. ...

## 输出要求
- 控制在 N 字以内
- 不要添加格式标记
```

**占位符替换**：`{param_name}`（格式 B）/ `{{param_name}}`（格式 A）会被替换为 LLM 调用时传入的对应参数值。v1.2.0 起仅替换 manifest/frontmatter 中声明的参数，用户输入自动包裹在 `<user_input>` 标签中以防提示注入。未提供的可选参数占位符会被自动清理。

### 第三层渐进披露：资源文件

技能目录下若包含以下任一子目录，里面的文件可作为"按需加载"的资源：

```text
my_skill/
├── SKILL.md
├── references/         ← 长篇参考、规范文档
├── resources/          ← 模板、示例数据
├── scripts/            ← 脚本（仅供 LLM 阅读，不会执行）
└── data/               ← 静态数据（CSV、JSON 等）
```

任何注册的技能只要带上述子目录之一，KiraOS 就会自动注册一个名为 `read_skill_resource` 的工具：

```json
{
  "name": "read_skill_resource",
  "parameters": {
    "skill_name": "my_skill",
    "path": "references/deep_rules.md"
  }
}
```

执行 SKILL.md 主指令时，KiraOS 会把所有可用资源以 `<resources>...</resources>` 块附加在指令末尾，引导 LLM 按需调用。资源读取严格沙箱化：只允许上述四个子目录，禁止 `..` 路径穿越，单文件硬上限 200 KB，仅接受 UTF-8 文本。

这正是 Claude Skills 的"三层渐进披露"模型：
1. **第一层**: 启动时加载 metadata（`name + description`，~100 token）
2. **第二层**: LLM 触发技能时返回完整 SKILL.md 正文
3. **第三层**: 技能正文里告诉 LLM "需要 X 时调用 read_skill_resource 读 references/X.md"

### 完整示例：塔罗牌占卜

这是内置的示例技能（v2.0.0 起统一使用 SKILL.md 格式 A），展示了完整的技能结构。

**目录结构**：

```text
data/skills/tarot_reading/
└── SKILL.md
```

**SKILL.md**：

````markdown
---
name: tarot_reading
description: 塔罗牌占卜
trigger: 当用户明确要求进行塔罗牌占卜、算命、或者预测运势时调用此工具。
exclude: 用户只是随口提到占卜但没有要求执行；用户问星座运势应走 daily_fortune。
command: /tarot
parameters:
  type: object
  properties:
    question:
      type: string
      description: 用户想要占卜的问题
  required:
    - question
---

# 塔罗牌占卜技能

## 角色设定
你现在是一位神秘的塔罗牌占卜师。你拥有敏锐的洞察力，能通过塔罗牌解读命运的线索。

## 执行步骤

1. **抽牌**：从大阿尔卡纳牌中随机选择一张（正位或逆位）：
   愚者、魔术师、女祭司、皇后、皇帝、教皇、恋人、战车、力量、隐士、
   命运之轮、正义、倒吊人、死神、节制、恶魔、塔、星星、月亮、太阳、审判、世界

2. **解读**：根据用户的问题「{{question}}」和抽到的牌面含义给出专业的占卜解读。
   - 先描述牌面的象征意义
   - 再结合用户的问题进行具体分析
   - 给出建议和展望

3. **语气**：保持神秘而温暖的语气，适度使用占卜相关的意象。

## 输出要求
- 只输出占卜结果的内容本身
- 控制在 300 字以内
````

**运行效果**：当用户说「帮我算算今天的运势」时，LLM 调用 `tarot_reading(question="今天的运势")`，插件加载 SKILL.md 正文、把 `{{question}}` 替换成 `<user_input>今天的运势</user_input>`，作为 tool_result 返回给主 LLM 阅读并执行。

> 🔄 **从老格式迁移**：v2.0.0 之前使用的 `manifest.json + instruction.md`（占位符 `{question}`）继续完全兼容——发现器优先识别 SKILL.md，缺失才回退老格式。如要把老技能迁到新格式，把 manifest 字段改写成 YAML frontmatter 拼到 instruction.md 头部、把 `{xxx}` 替换为 `{{xxx}}`、保存为 `SKILL.md`、删掉原来的两个文件即可。

### 示例技能

仓库 `skills/` 目录下包含以下示例技能，首次使用时将它们复制到 `data/skills/` 即可：

| 技能 | 说明 | 斜杠命令 | 触发示例 |
|------|------|----------|----------|
| `tarot_reading` | 塔罗牌占卜 | `/tarot` | 「帮我算算今天的运势」 |
| `daily_fortune` | 每日星座运势 | `/fortune` | 「我是双鱼座，看看今天运势」 |
| `story_continue` | 接龙续写故事 | `/story` | 「帮我续写这个故事：从前有座山……」 |
| `emoji_interpret` | Emoji 解读翻译 | `/emoji` | 「🥺👉👈 这是什么意思？」 |
| `nickname_generator` | 趣味昵称生成器 | `/nickname` | 「帮我取个二次元风格的网名」 |
| `personality_test` | 趣味性格测试 | `/personality` | 「我想做个性格小测试」 |

---

## 配置项

通过 WebUI 配置页面或插件 schema.json 设置：

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_events_per_user` | integer | 10 | 每次 LLM 上下文中注入的最大事件数 |
| `max_profiles_per_user` | integer | 50 | 每个用户允许的最大画像条目数 |
| `max_event_keep` | integer | 100 | 数据库中每个用户保留的最大事件数（超出自动清理） |
| `max_context_chars` | integer | 500 | 每用户注入的记忆上下文最大字符数（0 = 不限制），超限时按分类优先级截断 |
| `inject_categories` | list | `["basic"]` | 每轮自动注入到 system prompt 的画像分类。其余分类不注入但通过 `memory_query(category=...)` 可查。设为 `["*"]` 或 `["all"]` 恢复 v1.2.0 之前"全量注入"的行为 |
| `memory_auditor_enabled` | switch | `false` | 启用主动记忆审计员（C 层）。开启后每轮额外消耗一次 fast LLM 调用扫描用户消息提取漏记事实。⚠️ **隐私**：开启即向审计员模型发送用户最新消息 + 该用户**最多 50 条已有画像**（昵称/地点/关系等 PII）。若 `memory_auditor_model_uuid` 指向了与主 LLM 不同的 provider，这些数据就会同时共享给那个 provider；启用前请确认其数据处理策略可接受 |
| `memory_auditor_model_uuid` | model_select | `""` | 审计员使用的 LLM 模型（WebUI 渲染为下拉选择器，从已配置的 LLM 列表里选）。留空（推荐）走默认 fast LLM；想固定到某个具体模型时再选 |
| `memory_auditor_skip_keywords` | list | `["别记", "忘了它", "随便说说", "随便聊", "开玩笑", "假设说", "假如", "假设"]` | 用户消息命中任一关键词时审计员整个跳过本轮（隐私守门） |
| `skills_dir` | string | `null` | 技能目录路径（为空时自动使用 `data/skills/`） |
| `disabled_skills` | list | `[]` | 要禁用的技能名称列表，如 `["tarot_reading", "daily_fortune"]` |
| `enable_slash_commands` | switch | `false` | 是否允许用户通过 `/command` 触发技能，默认关闭 |
| `webui_port` | integer | 0 | Memory WebUI 监听端口（0 = 禁用）。推荐值：8765 |
| `webui_host` | string | `127.0.0.1` | WebUI 绑定地址。`127.0.0.1` 仅本机访问 |
| `webui_token` | string | `""` | WebUI 访问令牌（留空则无认证） |

所有整型配置项最小值为 0。

---

## 数据存储

### 数据库

- **路径**: `data/memory/kiraos.db`
- **引擎**: SQLite (WAL 模式 + `synchronous=NORMAL`)
- **并发模型**（v2.0.0）: **每线程独立连接** + **写共享一把 Lock，读完全无锁**。WAL 支持多读单写，读操作不再阻塞 LLM hook 与 WebUI 高频轮询互相争抢。所有曾发出过的连接进 registry，`close()` 时统一关闭。

**表结构**：

```sql
-- 用户画像（主键: user_id + memory_key）
CREATE TABLE user_profiles (
    user_id      TEXT NOT NULL,
    memory_key   TEXT NOT NULL,
    memory_value TEXT NOT NULL,           -- 上限 500 字符，超出自动截断
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    confidence   REAL DEFAULT 0.5,
    category     TEXT DEFAULT 'basic',
    expires_at   INTEGER DEFAULT NULL,    -- unix epoch（旧 ISO 字符串自动迁移）
    PRIMARY KEY (user_id, memory_key)
);

-- 事件日志
CREATE TABLE event_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    event_summary TEXT NOT NULL,           -- 上限 1000 字符
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    tag           TEXT DEFAULT NULL        -- 可选事件标签，如 milestone/daily/mood
);

-- 事件日志索引（按用户 + 时间倒序，加速上下文查询）
CREATE INDEX idx_event_logs_user
    ON event_logs (user_id, created_at DESC);
```

### 技能文件

- **路径**: `data/skills/<skill_name>/`
- **首次启动**: 若 `data/skills/` 不存在会自动创建
- **指令缓存**: `instruction.md` 在首次触发后缓存至内存，后续调用零 IO

---

## 架构概览

```text
KiraOS Plugin (main.py)
├── UserMemoryDB (db.py)           ← SQLite 读写（thread-local conn + auto-migrate）
│   ├── user_profiles 表            ← 长期画像（分类 + 置信度 + TTL）
│   ├── event_logs 表               ← 短期事件（含 tag）
│   ├── upsert_with_limit()        ← 原子 upsert + 冲突预检
│   ├── search_users()             ← 跨表搜索 user_id/profile/event
│   └── export_all() / import_all()← JSON 备份与恢复
├── SkillRouter (skill_router.py)  ← 技能发现与指令构建
│   ├── SkillInfo                   ← 元数据 + trigger/exclude/command + 指令缓存
│   ├── _parse_skill_md()          ← SKILL.md (YAML frontmatter) 解析
│   └── read_resource()            ← 资源文件沙箱化读取
├── WebUIServer (web_server.py)    ← Memory WebUI（REST API + SPA）
│   ├── TokenAuthMiddleware        ← Bearer 鉴权（compare_digest）
│   └── web/index.html              ← 单文件前端界面
├── 工具（向 LLM 注册）：
│   ├── memory_update()            ← 批量记忆操作（A 层：清单式触发 description）
│   ├── memory_query()             ← 按分类/用户查询记忆
│   ├── memory_clear()             ← 清除全部记忆
│   ├── consolidate_memory()       ← 事件 → 长期画像压缩
│   └── read_skill_resource()      ← 第三层渐进披露（仅当有技能带资源时注册）
└── 钩子：
    ├── inject_context (@on.llm_request)   ← 注入记忆段 + memory_hint + 技能列表（A+B 层）
    ├── handle_slash_command (@on.im_message)  ← 斜杠命令拦截
    └── schedule_audit (@on.step_result)   ← 审计员调度（C 层，缺省关闭）
                                              ↓ asyncio.create_task
                                            _run_auditor() ← fast LLM JSON 提取 + 写库
```

**生命周期**：

| 阶段 | 操作 |
|------|------|
| `initialize()` | 自动禁用内置 Simple Memory → 初始化数据库（含 expires_at 字符串→epoch / 新增 tag 列等迁移）→ 扫描技能目录（识别 SKILL.md / 老格式）→ 注册技能工具 → 若任一技能带资源子目录则注册 `read_skill_resource` → 构建斜杠命令映射 → 启动 WebUI（含 5s 就绪检测） |
| 运行中 | `inject_context` 钩子每轮注入记忆段 + memory_hint + 技能列表；`handle_slash_command` 拦截斜杠命令；LLM 按需调用记忆工具和技能工具；若审计员开启，`schedule_audit` 在每个 batch event 异步触发一次 `_run_auditor` 兜底捕获漏记；WebUI 提供 REST API |
| `terminate()` | 停止 WebUI → **排空 `_auditor_tasks`**（5s gather；超时则 cancel 后再 gather 2s 让 in-flight 任务清理收尾，避免对已关闭 DB 写入）→ 注销所有动态工具（技能 + read_skill_resource）→ 关闭数据库（统一关闭所有 thread-local 连接）。整体最长 ~7s |

---

## 故障排除

### LLM 调用时报 TypeError

**症状**：日志出现类似 `TypeError: inject_context() takes 3 positional arguments but 4 were given`

**原因**：插件版本过旧，`@on.llm_request()` 钩子签名未适配最新 Kira（现在传递 3 个位置参数 `event, request, tag_set`）。

**解决**：更新到 v1.1.0+。

### memory_update 工具调用失败

**症状**：LLM 调用 `memory_update` 时报参数错误。

**可能原因**：内置 Simple Memory 与 KiraOS 同时启用，两者都注册了 `memory_update` 但参数不同。

**解决**：KiraOS v1.1.0 已自动处理此冲突。若仍有问题，手动在 WebUI 禁用 Simple Memory。

### 技能未被发现

**检查清单**：
1. 技能文件夹是否在 `data/skills/` 下（非 `skills/`）
2. 文件夹名是否以 `_` 或 `.` 开头（会被跳过）
3. 格式 A：是否有 `SKILL.md`，frontmatter 是否能被 YAML 解析（首行必须是 `---`）
4. 格式 B：是否同时包含 `manifest.json` 和 `instruction.md`
5. `name` 字段（frontmatter 或 manifest 里）是否存在且为非空字符串
6. 是否与其他技能重名（检查日志中的 `Duplicate skill name` 警告）

### memory_update 报错 "set <key> requires explicit 'confidence' (0-1)"

**症状**：v2.0.0 起，`set` 操作不带 `confidence` 字段会被拒绝。

**原因**：M3 改动让 LLM 主动判断"我有多确定"，避免无脑用 0.5 默认值。

**解决**：在调用 `memory_update` 时为每条 `set` 显式传 `confidence`（确定信息 ≥0.8，不确定 0.3-0.5）。如果是从老版本迁移的 LLM prompt，更新 prompt 模板让 LLM 知道这是必填项。

### 审计员配置后没有触发

**检查清单**：

1. `memory_auditor_enabled` 是否设为 `true`
2. 项目里 `ctx.get_default_fast_llm_client()` 是否返回有效 client（检查 provider 配置里有没有标 fast 的模型）
3. 用户消息是否命中 `memory_auditor_skip_keywords` 里的某个词（如"别记"）→ 命中即跳过本轮
4. 用户消息是否过短（< 2 字符）→ 直接跳过
5. 看日志里 `[auditor]` 前缀的输出，会写明扫描结果或失败原因

### WebUI API 请求返回 401（之前还能用 `?token=`）

**症状**：脚本/curl 用 `?token=xxx` 直连 `/api/users` 等端点，v2.0.0 起返回 401。

**原因**：#10 安全收紧，移除了查询参数 token fallback（避免泄露到 server log / referer / 浏览器历史）。SPA 前端不受影响（自动适配为 Bearer 头）。

**解决**：脚本改为：

```bash
curl -H "Authorization: Bearer <token>" http://host:port/api/users
```

### 升级到 v2.0.0 后 expires_at 显示异常

**症状**：升级后 WebUI 详情页过期日期显示空或异常。

**原因**：`user_profiles.expires_at` 列从 ISO 字符串改为 INTEGER (unix epoch)，首次启动会自动迁移。如果迁移日志没出现 `Migrated N expires_at value(s)`，说明可能没生效。

**解决**：检查启动日志确认迁移完成。WebUI API 输出会自动把 epoch 转回 `YYYY-MM-DD` 字符串，前端无感。如有外部工具直读数据库且按字符串解析过期时间，需要更新为读 INTEGER。

---

## 更新日志

### v2.0.0

本次为大规模重构，**含若干破坏性变更**。

**⚠️ 破坏性变更**

- `memory_update` 的 `set` 操作 `confidence` 字段改为**必填**，缺失会被拒绝
- WebUI `/api/*` 不再接受 `?token=<token>` 查询参数，必须用 `Authorization: Bearer ...` 头（SPA 前端已自动适配）
- `user_profiles.expires_at` 列从 ISO 字符串改为 INTEGER unix epoch（首启自动迁移）

**主动记忆 (Active Recall, A+B+C)**

- **新增**：A 层 — `memory_update` description 改为清单式正向触发 + "宁记错不漏过"，6 类触发词清单（身份/地点/职业/关系/偏好/经历）显著提升主动记忆率
- **新增**：B 层 — 每轮 system prompt 末尾追加 `Prompt(name="memory_hint")`，提醒 LLM 检查用户是否提到自身事实
- **新增**：C 层 — 可选审计员 LLM（缺省关闭），通过 `@on.step_result()` 异步触发 fast LLM 扫描漏记事实并写库
  - 每个 batch event 只审一次（per-event dedup via `id(event)`）
  - 黑名单关键词命中时不发 LLM 请求（隐私守门）
  - JSON 输出（不走 tool calling）+ 容忍 prose / code fence
  - confidence 封顶 0.7，不会盖过主 LLM 的高置信度判断
  - 15s 超时 + 失败静默隔离
- **新增**：3 项配置 — `memory_auditor_enabled` / `memory_auditor_model_uuid` / `memory_auditor_skip_keywords`

**记忆系统增强**

- **新增 (M1)**：`memory_update` batch 内对 `(op, key, target_uid)` 自动去重，最后一条胜出（防 LLM 反复改主意）。事件不去重
- **新增 (M2)**：profile value 上限 500 字符，event summary 上限 1000 字符，超出自动截断 + log warning
- **新增 (M3)**：`set` 操作 `confidence` 必填（破坏性，见上）
- **新增 (M4)**：WebUI 备份/恢复 — `GET /api/export` JSON 快照、`POST /api/import?mode=merge|upsert|replace`，概览页加按钮 + 模式下拉
- **新增 (M5)**：群聊场景 `memory_update` / `memory_query` 接受 per-op `user_id`，校验白名单为当前对话发言者集（防跨用户写）
- **新增 (M6)**：`upsert_with_limit` 写入冲突预检 — 现值置信度比新值高 > 0.2 且值不同时拒绝写入并返回 hint，可用 `force: true` 强制覆盖
- **新增 (M7)**：`event_logs.tag` 列（自动迁移），事件可标 `milestone`/`daily`/`mood` 等；`get_recent_events(tags=[...])` 支持过滤；注入上下文显示为 `2026-04-01#milestone ...`
- **新增 (M8)**：WebUI 跨表搜索 — `GET /api/users?q=<term>` 同时匹配 user_id / 画像 value / 事件 summary，结果带 `match_in` 和 `snippet`；前端搜索框改为 200ms debounce 服务端搜索
- **新增 (M10)**：`consolidate_memory` 工具 — 把累积的事件压缩为长期画像，让 LLM 在同一轮 tool-loop 里识别 ≥3 次的稳定模式

**Skills 系统增强（Claude Skills 风格）**

- **新增 (#1)**：SKILL.md 单文件格式（YAML frontmatter + 正文），向后兼容老 manifest.json + instruction.md 格式
- **新增 (#2)**：第三层渐进披露 — 技能目录下的 `references/` `resources/` `scripts/` `data/` 子目录可作按需加载资源；自动注册 `read_skill_resource` 工具（沙箱化：禁止 `..` 穿越，单文件 ≤ 200KB）
- **改造 (#3)**：占位符语法 — SKILL.md 用 `{{param}}`（Jinja 风格，避免与 JSON 例子冲突），老格式保持 `{param}`
- **改造 (#5)**：DB 并发模型 — 单连接 + 全局锁 → 每线程独立连接 + 写锁分离，读完全无锁
- **改造 (#6)**：`memory_update` 的 set 从三次查询合并为单次 `upsert_with_limit` 原子事务
- **改造 (#7)**：`expires_at` 改 INTEGER epoch（破坏性，见上）

**安全 / 工程质量**

- **改造 (#10)**：WebUI Bearer token 比较改用 `secrets.compare_digest`；移除 `?token=` 查询参数 fallback（破坏性）
- **修复 (#11)**：`inject_context` 找不到 `memory` 段时改为新建独立段，不再追加到 `tools` 段污染
- **修复 (#12)**：`WebUIServer.start()` 检测端口占用即抛 `RuntimeError`，不再静默吞掉 uvicorn 的 `SystemExit`

**配置项变更**

- 新增 `inject_categories`（M8 注入分类）、`memory_auditor_enabled` / `memory_auditor_model_uuid` / `memory_auditor_skip_keywords`（C 审计员）

**数据库 schema（自动迁移）**

- `user_profiles.expires_at`：DATETIME → INTEGER unix epoch
- `event_logs` 新增 `tag TEXT DEFAULT NULL` 列

**示例技能更新**

- **迁移**：bundled `skills/` 下的 6 个示例技能（tarot_reading / daily_fortune / story_continue / emoji_interpret / nickname_generator / personality_test）从 `manifest.json + instruction.md` 老格式统一迁到 SKILL.md 单文件格式
- 占位符语法从 `{param}` 升级为 `{{param}}`（Jinja 风格）
- 删除原 12 个老格式文件
- 用户自定义的老格式技能继续完全兼容

**元数据**

- `manifest.json` 的 `version`：`1.2.0` → `2.0.0`
- README 文档头版本标注：`1.3.0` → `2.0.0`

**迁移指南（v1.3.x → v2.0.0）**

1. LLM tool 调用代码：所有 `memory_update` 的 `set` 必须显式带 `confidence`
2. 直连 WebUI API 的脚本：`?token=` 改为 `Authorization: Bearer ...` 头
3. 配置文件：无需手动改；如需启用审计员，把 `memory_auditor_enabled` 设为 `true`
4. 数据库：无需手动迁移，首启自动完成。建议升级前先 `GET /api/export` 备份
5. 示例技能：bundled 示例已迁到 SKILL.md；用户自定义老格式技能继续兼容、无需迁移

### v1.3.0

**WebUI 重构与增强**

- **新增**：概览首页 — 统计面板显示累计用户数、画像记录数、事件记录数，实时刷新
- **新增**：可折叠侧边栏导航 — 「概览」和「记忆管理」两个导航项，记忆管理展开后显示用户搜索与列表
- **新增**：实时更新功能 — 1 秒轮询间隔，支持一键开关，使用 JSON 指纹去重避免页面闪跳
- **新增**：新建用户 — 侧边栏「＋ 新建用户」按钮，输入 ID 后进入空详情页，首次保存画像或事件时自动创建
- **新增**：添加画像 — 详情页「＋ 添加画像」按钮，行内表单支持键、值、置信度、分类
- **新增**：添加事件 — 详情页「＋ 添加事件」按钮，行内表单输入事件内容
- **新增**：事件行内编辑 — 每条事件的 ✎ 编辑按钮，点击切换为编辑模式即时修改
- **新增**：实时更新守护 — 编辑/添加画像或事件期间自动暂停实时刷新，避免打断操作
- **新增**：轮询日志过滤 — 抑制 `/api/stats` 和 `/api/users` 高频访问日志噪音
- **调整**：页面标题更名为 "KiraOS WebUI"

**REST API 扩展**

- **新增**：`GET /api/stats` — 获取统计数据（用户数、画像数、事件数）
- **新增**：`POST /api/users/{user_id}/events` — 添加事件记录
- **新增**：`PUT /api/users/{user_id}/events/{event_id}` — 更新事件内容

**数据库扩展**

- **新增**：`get_stats()` 方法 — 一次查询返回用户数、画像数、事件数
- **新增**：`update_event()` 方法 — 按 ID 更新事件内容，支持可选的 user_id 校验

### v1.3.0-beta

**Memory WebUI**

- **新增**：内置 Memory WebUI — 基于 Starlette 的可视化记忆管理界面，支持浏览器中查看、编辑、删除用户画像和事件
- **新增**：REST API — 完整的用户记忆 CRUD 接口（列出用户、查询/更新/删除画像、删除事件、清除用户记忆）
- **新增**：Bearer Token 认证中间件 — 可选的 API 访问控制，保护公网部署安全
- **新增**：单文件 SPA 前端（`web/index.html`）— 毛玻璃风格 UI，支持亮色/暗色主题自动切换与手动切换
- **新增**：3 项配置 — `webui_port`（监听端口）、`webui_host`（绑定地址）、`webui_token`（访问令牌）
- **新增**：WebUI 生命周期自动管理 — 插件初始化时自动启动，终止时自动停止

**数据库扩展**

- **新增**：`list_users()` 方法 — 汇总所有用户及其画像/事件计数
- **新增**：`delete_event()` 方法 — 按 ID 删除单条事件日志
- **新增**：`get_events_with_id()` 方法 — 返回带 ID 的事件列表，供 WebUI 使用

### v1.2.0

**记忆系统增强**

- **新增**：画像分类系统 — 支持 `basic`（基本信息）/ `preference`（偏好）/ `social`（社交）/ `other`（其他）四种分类，上下文注入时按分类优先级排序
- **新增**：置信度机制 — 每条画像支持 `confidence`（0-1）标注，确定信息用 0.8+，不确定用 0.3-0.5，注入时显示置信度标记（✓ / ? / ~）
- **新增**：画像 TTL 过期 — 支持设置过期时间（如 `30d`、`7d`、`12h`），过期条目自动从查询结果中排除
- **新增**：`memory_query` 工具 — 用户可主动查询自己的全部记忆画像和近期事件
- **新增**：`memory_clear` 工具 — 用户可请求清除全部记忆数据
- **新增**：`max_context_chars` 配置项 — 限制每用户注入的记忆上下文字符数，超限时按分类优先级截断
- **新增**：`memory_update` 返回值附带当前画像摘要，帮助 LLM 检测信息矛盾
- **优化**：数据库自动迁移 — 旧版数据库表自动添加 `confidence`、`category`、`expires_at` 列，无需手动操作

**技能路由增强**

- **新增**：manifest 三字段扩展 — `trigger`（精确触发条件）、`exclude`（排除条件）、`command`（斜杠命令）
- **新增**：`tool_description` 属性 — 工具注册时优先使用 `trigger` 字段，`description` 回归为简短标题
- **新增**：排除条件守卫 — 技能执行时自动在指令前插入 `exclude` 条件提示，减少误触发
- **新增**：参数安全替换 — 仅替换 manifest 中声明的参数占位符，用户输入包裹在 `<user_input>` 标签中防止提示注入
- **新增**：斜杠命令拦截 — 支持通过 `/command` 形式触发技能（如 `/tarot`、`/fortune`），默认关闭，需在配置中启用
- **新增**：`enable_slash_commands` 配置项 — 控制是否允许斜杠命令触发
- **新增**：`disabled_skills` 配置项 — 指定禁用的技能列表
- **新增**：技能热重载 — 支持运行时重新扫描和注册技能

**示例技能更新**

- **优化**：所有 6 个示例技能的 manifest 新增 `trigger`、`exclude`、`command` 字段
- **调整**：`story_continue` 输出字数限制从 150~250 字放宽到 200~400 字
- **调整**：`tarot_reading` 输出字数限制从 200 字放宽到 300 字
- **修正**：`nickname_generator` 优化关键词为空时的指令描述

### v1.1.0

- **修复**：`@on.llm_request()` 钩子签名适配最新 Kira（新增 `tag_set` 参数）
- **修复**：技能执行器函数签名兼容额外位置参数
- **新增**：自动检测并禁用内置 Simple Memory 插件，避免工具名与 Hook 冲突
- **新增**：5 个示例技能（每日星座运势、接龙续写故事、Emoji 解读、昵称生成器、趣味性格测试）

### v1.0.0

- 初始版本：用户记忆系统 + 技能路由系统
- 示例技能：塔罗牌占卜
