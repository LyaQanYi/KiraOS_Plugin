# KiraOS 插件文档

> **插件 ID**: `kira_plugin_kiraos`  
> **版本**: 1.0.0  
> **作者**: LyaQanYi

KiraOS 是 Kira 的 OS 级插件，整合了两大核心能力：

| 能力 | 类比 | 说明 |
|------|------|------|
| **用户记忆 (User Memory)** | RAM | 基于 SQLite 的按用户持久化画像与事件日志，自动注入 LLM 上下文 |
| **技能路由 (Skill Router)** | 程序加载器 | 渐进式工具发现——启动时加载轻量 manifest，运行时按需注入完整指令 |

---

## 目录

- [快速开始](#快速开始)
- [用户记忆系统](#用户记忆系统)
  - [工作原理](#记忆---工作原理)
  - [memory_update 工具](#memory_update-工具)
  - [操作类型详解](#操作类型详解)
  - [上下文自动注入](#上下文自动注入)
- [技能路由系统](#技能路由系统)
  - [工作原理](#技能---工作原理)
  - [创建自定义技能](#创建自定义技能)
  - [manifest.json 规范](#manifestjson-规范)
  - [instruction.md 规范](#instructionmd-规范)
  - [完整示例：塔罗牌占卜](#完整示例塔罗牌占卜)
- [配置项](#配置项)
- [数据存储](#数据存储)
- [架构概览](#架构概览)

---

## 快速开始

KiraOS 作为插件需要放置在 `.\core\plugin` 目录下，并在WebUI启用。启动 Kira 后插件会自动：

1. 初始化 SQLite 记忆数据库 (`data/memory/kiraos.db`)
2. 扫描 `data/skills/` 目录，发现并注册所有技能工具
3. 在每次 LLM 调用前自动注入用户记忆上下文

```
data/
├── memory/
│   └── kiraos.db          ← 自动创建
└── skills/
    └── tarot_reading/     ← 示例技能
        ├── manifest.json
        └── instruction.md
```

---

## 用户记忆系统

### 记忆 - 工作原理

记忆系统分为两层存储：

- **用户画像 (Profile)**：长期键值对，如 `昵称=小明`、`城市=北京`
- **事件日志 (Event)**：按时间排序的短期事件记录，如 `完成半马`、`通过面试`

LLM 在对话中识别到有价值的用户信息时，会自动调用 `memory_update` 工具进行记忆存储。闲聊不会触发记忆。

### memory_update 工具

这是 LLM 用于管理用户记忆的唯一工具，支持批量操作。

**工具签名**：

```json
{
  "name": "memory_update",
  "parameters": {
    "operations": [
      {
        "op": "set | event | del",
        "key": "画像键名（set/del 时必填）",
        "value": "画像值（set 时）或事件描述（event 时）"
      }
    ]
  }
}
```

### 操作类型详解

#### `set` — 设置/更新用户画像

新增或更新一条画像键值对。若 key 已存在则更新值，若 key 为新增则检查是否达到上限。

```json
{"op": "set", "key": "昵称", "value": "小明"}
{"op": "set", "key": "城市", "value": "北京"}
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
    {"op": "set", "key": "昵称", "value": "小明"},
    {"op": "set", "key": "城市", "value": "北京"},
    {"op": "event", "value": "完成半马"}
  ]
}
```

返回：`已完成 3 项记忆操作: set 昵称=小明; set 城市=北京; event: 完成半马`

### 上下文自动注入

每次 LLM 调用前，插件通过 `llm_request` 钩子自动将用户记忆注入系统提示：

```
[user_123] 昵称=小明 | 城市=北京 | 职业=工程师
[user_123:events] 2025-03-01 完成半马 | 2025-02-28 通过面试
```

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
  "description": "触发条件描述（帮助 LLM 判断何时调用）",
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
| `description` | string | 否 | 描述技能的用途和触发条件，帮助 LLM 决定是否调用 |
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

**占位符替换**：`{param_name}` 会被替换为 LLM 调用时传入的对应参数值。

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
  "description": "当用户明确要求进行塔罗牌占卜、算命、或者预测运势时调用此工具。",
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
- 控制在 200 字以内
```

**运行效果**：当用户说「帮我算算今天的运势」时，LLM 调用 `tarot_reading(question="今天的运势")`，插件返回替换后的完整指令，LLM 直接阅读并按指令生成占卜结果。

---

## 配置项

通过 WebUI 配置页面或插件 schema.json 设置：

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_events_per_user` | integer | 10 | 每次 LLM 上下文中注入的最大事件数 |
| `max_profiles_per_user` | integer | 50 | 每个用户允许的最大画像条目数 |
| `max_event_keep` | integer | 100 | 数据库中每个用户保留的最大事件数（超出自动清理） |
| `skills_dir` | string | `data/skills/` | 技能目录路径 |

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
├── UserMemoryDB (db.py)           ← SQLite 读写
│   ├── user_profiles 表            ← 长期画像
│   └── event_logs 表               ← 短期事件
├── SkillRouter (skill_router.py)  ← 技能发现与指令构建
│   └── SkillInfo                   ← 单个技能的元数据 + 指令缓存
├── memory_update()                ← LLM 工具：批量记忆操作
└── inject_context()               ← LLM 钩子：自动注入记忆 + 技能列表
```

**生命周期**：

| 阶段 | 操作 |
|------|------|
| `initialize()` | 初始化数据库 → 扫描技能目录 → 注册技能工具 |
| 运行中 | `inject_context` 钩子注入记忆；LLM 按需调用 `memory_update` 和技能工具 |
| `terminate()` | 注销技能工具 → 关闭数据库连接 |
