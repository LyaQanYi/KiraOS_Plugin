# KiraOS 插件文档

> **插件 ID**: `kira_plugin_kiraos`  
> **版本**: 1.2.0  
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
  - [memory_clear 工具](#memory_clear-工具)
  - [上下文自动注入](#上下文自动注入)
- [技能路由系统](#技能路由系统)
  - [工作原理](#技能---工作原理)
  - [创建自定义技能](#创建自定义技能)
  - [manifest.json 规范](#manifestjson-规范)
  - [instruction.md 规范](#instructionmd-规范)
  - [示例技能](#示例技能)
- [配置项](#配置项)
- [数据存储](#数据存储)
- [架构概览](#架构概览)
- [故障排除](#故障排除)
- [更新日志](#更新日志)

---

## 安装

将整个 `KiraOS_Plugin` 文件夹放入以下目录即可：

```text
core/plugin/builtin_plugins/
└── KiraOS_Plugin/
    ├── __init__.py
    ├── main.py
    ├── db.py
    ├── skill_router.py
    ├── manifest.json
    └── schema.json
```

两种方式均兼容，无需修改任何代码。重启 Kira 后插件会自动被发现并加载。可在 WebUI 的插件管理页面中启用/禁用及调整配置。

> ⚠️ **注意**: 只有上述两个目录是有效的插件安装路径。**不要**将插件放在 `core/plugin/` 下——Kira 的插件发现机制不会扫描该目录。

---

## 快速开始

安装后启动 Kira，插件会自动：

1. **检测并禁用内置记忆插件**（Simple Memory），避免工具名和 Hook 冲突
2. 初始化 SQLite 记忆数据库 (`data/memory/kiraos.db`)
3. 扫描 `data/skills/` 目录，发现并注册所有技能工具
4. 在每次 LLM 调用前自动注入用户记忆上下文

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

- **用户画像 (Profile)**：长期键值对，如 `昵称=小明`、`城市=北京`
- **事件日志 (Event)**：按时间排序的短期事件记录，如 `完成半马`、`通过面试`

LLM 在对话中识别到有价值的用户信息时，会自动调用 `memory_update` 工具进行记忆存储。闲聊不会触发记忆。

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
        "confidence": "0-1 的置信度（可选，默认 0.5）",
        "ttl": "过期时间，如 30d、7d、12h（可选）"
      }
    ]
  }
}
```

### 操作类型详解

#### `set` — 设置/更新用户画像

新增或更新一条画像键值对。若 key 已存在则更新值，若 key 为新增则检查是否达到上限。

```json
{"op": "set", "key": "昵称", "value": "小明", "category": "basic", "confidence": 0.9}
{"op": "set", "key": "城市", "value": "北京", "category": "basic", "confidence": 0.8}
{"op": "set", "key": "最近在减肥", "value": "是", "category": "other", "confidence": 0.5, "ttl": "30d"}
```

#### `event` — 记录事件

追加一条事件日志。超出保留上限后自动删除最旧的记录。

```json
{"op": "event", "value": "完成了第一次半程马拉松"}
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
    {"op": "set", "key": "城市", "value": "北京", "category": "basic", "confidence": 0.8},
    {"op": "event", "value": "完成半马"}
  ]
}
```

返回：`已完成 3 项记忆操作: set 昵称=小明 [basic]; set 城市=北京 [basic]; event: 完成半马`
并附带当前画像摘要，帮助 LLM 检测矛盾信息。

### memory_query 工具

用户主动查询自身记忆时使用，返回按分类分组的完整画像和近期事件。

**触发示例**：「你记得我什么？」「你知道我的信息吗？」「我的画像」

### memory_clear 工具

清除用户全部记忆数据（画像 + 事件日志）。仅当用户明确要求时调用。

**触发示例**：「忘记我」「清除我的记忆」「删除我的所有信息」

### 上下文自动注入

每次 LLM 调用前，插件通过 `llm_request` 钩子自动将用户记忆注入系统提示。v1.2.0 起按分类分组并标注置信度：

```text
[user_123:basic] 昵称=小明(✓) | 城市=北京(✓)
[user_123:preference] 喜欢猫(?) | 偏好甜食(~)
[user_123:events] 2025-03-01 完成半马 | 2025-02-28 通过面试
```

**置信度标记**：`✓` = 高（≥0.8）、`?` = 中（≥0.5）、`~` = 低（<0.5）

当总字符数超过 `max_context_chars` 限制时，按分类优先级（basic > preference > social > other > events）截断。

注入目标为系统提示中 `name="memory"` 的部分，若不存在则追加到末尾。

---

## 技能路由系统

### 技能 - 工作原理

技能路由采用**渐进式披露 (Progressive Disclosure)** 模式，灵感来自 Claude 的 Skill 系统：

```
┌─ 启动阶段 ──────────────────────────────┐
│  扫描 data/skills/ 目录                   │
│  加载每个技能的 manifest.json（轻量元数据）│
│  注册为 LLM 可用工具                      │
└────────────────────────────────────────────┘
              │
              ▼ LLM 决定调用某个技能
┌─ 运行阶段 ──────────────────────────────┐
│  按需加载 instruction.md（完整执行指令）  │
│  替换参数占位符 {arg_name}               │
│  附加用户记忆上下文（如有）              │
│  作为工具返回值返回给 LLM                │
│  LLM 在同一轮读取指令并直接执行          │
│  零额外 API 调用                         │
└────────────────────────────────────────────┘
```

**核心优势**：指令作为 tool_result 返回，LLM 在同一个 tool-loop 轮次中读取并执行，无需额外的 API 调用。

### 创建自定义技能

在 `data/skills/` 下创建子目录，包含以下两个文件：

```
data/skills/
└── my_skill/
    ├── manifest.json     ← 工具定义（必需）
    └── instruction.md    ← 执行指令（必需）
```

> 以 `_` 或 `.` 开头的目录会被跳过。

### manifest.json 规范

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

### instruction.md 规范

技能的完整执行指令。支持 `{参数名}` 占位符，运行时会被替换为实际参数值。

```markdown
# 技能标题

## 执行步骤
1. 根据参数 {param_name} 执行...
2. ...

## 输出要求
- 控制在 N 字以内
- 不要添加格式标记
```

**占位符替换**：`{param_name}` 会被替换为 LLM 调用时传入的对应参数值。v1.2.0 起仅替换 manifest 中声明的参数，用户输入自动包裹在 `<user_input>` 标签中以防提示注入。未提供的可选参数占位符会被自动清理。

### 完整示例：塔罗牌占卜

这是内置的示例技能，展示了完整的技能结构。

**目录结构**：

```
data/skills/tarot_reading/
├── manifest.json
└── instruction.md
```

**manifest.json**：

```json
{
  "name": "tarot_reading",
  "description": "塔罗牌占卜",
  "trigger": "当用户明确要求进行塔罗牌占卜、算命、或者预测运势时调用此工具。",
  "exclude": "用户只是随口提到占卜但没有要求执行；用户问星座运势应走 daily_fortune。",
  "command": "/tarot",
  "parameters": {
    "type": "object",
    "properties": {
      "question": {
        "type": "string",
        "description": "用户想要占卜的问题"
      }
    },
    "required": ["question"]
  }
}
```

**instruction.md**：

```markdown
# 塔罗牌占卜技能

## 角色设定
你现在是一位神秘的塔罗牌占卜师。你拥有敏锐的洞察力，能通过塔罗牌解读命运的线索。

## 执行步骤

1. **抽牌**：从大阿尔卡纳牌中随机选择一张（正位或逆位）：
   愚者、魔术师、女祭司、皇后、皇帝、教皇、恋人、战车、力量、隐士、
   命运之轮、正义、倒吊人、死神、节制、恶魔、塔、星星、月亮、太阳、审判、世界

2. **解读**：根据用户的问题「{question}」和抽到的牌面含义给出专业的占卜解读。
   - 先描述牌面的象征意义
   - 再结合用户的问题进行具体分析
   - 给出建议和展望

3. **语气**：保持神秘而温暖的语气，适度使用占卜相关的意象。

## 输出要求
- 只输出占卜结果的内容本身
- 控制在 300 字以内
```

**运行效果**：当用户说「帮我算算今天的运势」时，LLM 调用 `tarot_reading(question="今天的运势")`，插件返回替换后的完整指令，LLM 直接阅读并按指令生成占卜结果。

### 示例技能

仓库 `skills/` 目录下包含以下示例技能，首次使用时将它们复制到 `data/skills/` 即可：

| 技能 | 说明 | 斜杠命令 | 触发示例 |
|------|------|----------|----------|
| `tarot_reading` | 塔罗牌占卜 | `/tarot` | 「帮我算算今天的运势」 |
| `daily_fortune` | 十二星座每日运势 | `/fortune` | 「我是双鱼座，看看今天运势」 |
| `story_continue` | 接龙续写故事 | `/story` | 「帮我续写这个故事：从前有座山……」 |
| `emoji_interpret` | Emoji 解读翻译 | `/emoji` | 「🥺👉👈 这是什么意思？」 |
| `nickname_generator` | 趣味昵称生成 | `/nickname` | 「帮我取个二次元风格的网名」 |
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
| `skills_dir` | string | `data/skills/` | 技能目录路径 |
| `disabled_skills` | list | `[]` | 要禁用的技能名称列表，如 `["tarot_reading", "daily_fortune"]` |
| `enable_slash_commands` | switch | `false` | 是否允许用户通过 `/command` 触发技能，默认关闭 |

所有整型配置项最小值为 0。

---

## 数据存储

### 数据库

- **路径**: `data/memory/kiraos.db`
- **引擎**: SQLite (WAL 模式)
- **线程安全**: 所有操作通过 `threading.Lock` 保护

**表结构**：

```sql
-- 用户画像（主键: user_id + memory_key）
CREATE TABLE user_profiles (
    user_id      TEXT NOT NULL,
    memory_key   TEXT NOT NULL,
    memory_value TEXT NOT NULL,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    confidence   REAL DEFAULT 0.5,
    category     TEXT DEFAULT 'basic',
    expires_at   DATETIME DEFAULT NULL,
    PRIMARY KEY (user_id, memory_key)
);

-- 事件日志
CREATE TABLE event_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    event_summary TEXT NOT NULL,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 技能文件

- **路径**: `data/skills/<skill_name>/`
- **首次启动**: 若 `data/skills/` 不存在会自动创建
- **指令缓存**: `instruction.md` 在首次触发后缓存至内存，后续调用零 IO

---

## 架构概览

```
KiraOS Plugin (main.py)
├── UserMemoryDB (db.py)           ← SQLite 读写（含自动迁移）
│   ├── user_profiles 表            ← 长期画像（分类 + 置信度 + TTL）
│   └── event_logs 表               ← 短期事件
├── SkillRouter (skill_router.py)  ← 技能发现与指令构建
│   └── SkillInfo                   ← 元数据 + trigger/exclude/command + 指令缓存
├── memory_update()                ← LLM 工具：批量记忆操作
├── memory_query()                 ← LLM 工具：查询用户记忆
├── memory_clear()                 ← LLM 工具：清除全部记忆
├── handle_slash_command()         ← 消息钩子：斜杠命令拦截
└── inject_context()               ← LLM 钩子：自动注入记忆 + 技能列表
```

**生命周期**：

| 阶段 | 操作 |
|------|------|
| `initialize()` | 自动禁用内置 Simple Memory → 初始化数据库（含自动迁移）→ 扫描技能目录 → 注册技能工具 → 构建斜杠命令映射 |
| 运行中 | `inject_context` 钩子注入记忆；`handle_slash_command` 拦截斜杠命令；LLM 按需调用记忆工具和技能工具 |
| `terminate()` | 注销技能工具 → 关闭数据库连接 |

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
3. 是否同时包含 `manifest.json` 和 `instruction.md`
4. `manifest.json` 中 `name` 字段是否存在且为非空字符串
5. 是否与其他技能重名（检查日志中的重复警告）

---

## 更新日志

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
