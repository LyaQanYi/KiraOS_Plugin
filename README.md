# KiraOS 插件文档

> **插件 ID**: `kira_plugin_kiraos`
> **版本**: 4.0.1
> **作者**: LyaQanYi
> **兼容**: KiraAI v2.x (dev branch)

KiraOS 是 Kira 的 OS 级插件，整合了两大核心能力：

| 能力 | 类比 | 说明 |
|------|------|------|
| **双脑记忆 (Dual-Brain Memory)** | 大脑 | 快/慢两套循环：FTS5 实时检索 + 后台海马体异步提取、反思、画像更新；TOML 文件作真相源，SQLite 作可重建索引 |
| **技能路由 (Skill Router)** | 程序加载器 | 渐进式工具发现——启动时加载轻量 manifest，运行时按需注入完整指令 |

> **v4 统一公告**：原独立的 `kira_plugin_hippocampus_memory`（海马体记忆插件）已完全合并至本插件。所有功能保留，数据自动迁移。详见 [从 Hippocampus Memory 插件迁移](#从-hippocampus-memory-插件迁移)。

> v3 相对 v2.x 的核心变化：把简陋的"两张 SQLite 表 + 单用户画像"换成了完整的双脑记忆系统（TOML 真相源 + SQLite 索引 + 后台海马体）。详见 [从 v2 迁移](#从-v2-迁移)。

---

## 目录

- [安装](#安装)
- [快速开始](#快速开始)
- [与内置 Simple Memory 插件的关系](#与内置-simple-memory-插件的关系)
- [双脑记忆系统](#双脑记忆系统)
  - [架构概览](#架构概览)
  - [LLM 工具清单](#llm-工具清单)
  - [上下文自动注入](#上下文自动注入)
  - [跨 adapter 身份关联](#跨-adapter-身份关联)
  - [事件记忆（Episode）](#事件记忆-episode)
  - [运行状态与最近回忆](#运行状态与最近回忆)
  - [海马体（慢系统）](#海马体慢系统)
  - [TOML 真相源](#toml-真相源)
- [Memory WebUI](#memory-webui)
- [技能路由系统](#技能路由系统)
- [配置项](#配置项)
- [数据存储](#数据存储)
- [从 v2 迁移](#从-v2-迁移)
- [从 Hippocampus Memory 插件迁移](#从-hippocampus-memory-插件迁移)
- [从 MaiBot 迁移记忆](#从-maibot-迁移记忆)
- [故障排除](#故障排除)

---

## 安装

KiraOS 是一个 **外部插件**——**不**随 KiraAI 主程序一起分发，需要单独安装到主程序的 `data/plugins/` 目录下。

> ⚠️ 装到对地方：插件目录最终必须落在 `<KiraAI 仓库根>/data/plugins/KiraOS_Plugin/`，而**不是** `core/plugin/builtin_plugins/`。后者是 KiraAI 自带插件的位置，不在那里部署。

### 方式 A：WebUI 一键装（推荐）

适合大多数用户。**依赖会自动安装**——`requirements.txt` 由 KiraAI 的 plugin installer 在装入时自动 `pip install`。

1. 打开 KiraAI WebUI → 插件管理页
2. 点 "Install from GitHub"（粘 repo URL）或 "Install from ZIP"（上传发布包）
3. 装入成功后，在插件列表里启用 KiraOS
4. 重启 KiraAI

### 方式 B：本地手动放置

适合想从源码部署、或不希望走网络下载的人。**依赖需要自己装**。

```bash
# 1. 把插件目录复制到 data/plugins/（目录名必须是 KiraOS_Plugin）
cp -r /path/to/KiraOS_Plugin <KiraAI 仓库根>/data/plugins/KiraOS_Plugin

# 2. 在主程序 venv 里装依赖
#    Windows:
<KiraAI 仓库根>\venv\Scripts\activate.bat
python -m pip install -r data\plugins\KiraOS_Plugin\requirements.txt

#    Linux / macOS:
source <KiraAI 仓库根>/venv/bin/activate
python -m pip install -r data/plugins/KiraOS_Plugin/requirements.txt

# 3. 在 WebUI 启用插件并重启 KiraAI
```

### 方式 C：开发者软链（仅本地改代码时用）

```bash
# Linux/macOS
ln -s /path/to/KiraOS_Plugin/source <KiraAI 仓库根>/data/plugins/KiraOS_Plugin

# Windows (管理员 cmd)
mklink /D <KiraAI 仓库根>\data\plugins\KiraOS_Plugin C:\path\to\KiraOS_Plugin\source
```

依赖按方式 B 第 2 步装。改代码后重启 KiraAI 即可生效。

### 启用后的自动行为

KiraAI 主程序自带一个轻量的内置"Simple Memory"插件（`kira_plugin_simple_memory`）。KiraOS 启用时会**自动**把它禁用——两套记忆系统同时往 system prompt 里写会打架。详见 [与内置 Simple Memory 插件的关系](#与内置-simple-memory-插件的关系)。

### 依赖

插件根目录附带 [`requirements.txt`](requirements.txt)，列出运行时需要的 Python 包：

| 包 | 角色 | 必需？ |
|----|------|--------|
| `tomli_w` | TOML 写入 | 是（硬依赖，没法降级） |
| `tomli` | TOML 读取（Py3.10） | Py3.10 必需；3.11+ 自动跳过（走 stdlib `tomllib`） |
| `jieba` | FTS5 中文分词 | 否（缺失时降级到字符级分词，中文召回质量打折但插件能跑） |

**自动安装**：通过 KiraAI WebUI 的 "Install from GitHub" 或 "Install from ZIP" 装这个插件时，会自动执行 `pip install -r requirements.txt`。

**手动安装**（直接 git clone / cp 到 `data/plugins/` 的场景）：
```bash
python -m pip install -r data/plugins/KiraOS_Plugin/requirements.txt
```

---

## 快速开始

启用插件后立刻可用的 6 个 LLM 工具：

| 工具 | 作用 |
|------|------|
| `memory_add` | 记一条记忆 |
| `memory_search` | 检索记忆 |
| `memory_update_entry` | 修改一条已有记忆 |
| `memory_remove` | 把一条记忆移入归档 |
| `profile_view` | 查看用户画像 |
| `profile_update` | 改用户画像（trait / fact / relationship） |

记忆按实体（user / group / channel）隔离存储。默认每条工具都会以"当前发言者"为缺省 `entity_id`，所以 LLM 通常不需要显式传 `entity_id`。

---

## 与内置 Simple Memory 插件的关系

KiraAI 主程序自带一个轻量的"Simple Memory"内置插件（`kira_plugin_simple_memory`）。KiraOS 提供的是它的**完整替代品**：

| 维度 | Simple Memory（内置） | KiraOS（本插件，外部） |
|------|---------------------|--------------------|
| 存储 | 单一 SQLite，key-value 画像 + 事件日志 | TOML 真相源 + SQLite 索引 |
| 实体维度 | 只有 user | user / group / channel / global |
| 检索 | 全部按分类塞 system prompt | FTS5 中文分词 + 时间衰减 + top-K 召回 |
| 写入 | LLM 显式调工具 | LLM 工具 + 后台海马体自动提取 |
| 反思 / 升维 | 无 | 海马体定期把零散 facts 升维成 reflection |

KiraOS **启用时会自动把 Simple Memory 禁用**（避免两套记忆同时注入 system prompt）。如果想切换回 Simple Memory，先在 WebUI 禁用 KiraOS，再启用 Simple Memory。

---

## 双脑记忆系统

### 架构概览

```
                    用户消息                LLM 响应
                       │                       │
                       ▼                       ▼
                ┌─────────────────────────────────┐
                │ @on.step_result feed_hippocampus │
                │ → memory_manager.update_memory   │
                └────────────────┬─────────────────┘
                                 │ 每 N 条 (hippocampus_threshold)
                                 ▼
   ┌────────────────────────────────────────────────────────┐
   │ 慢系统（海马体）— 异步后台任务                          │
   │  1. 提取事实  ─────────────────────► MemoryExtractor    │
   │  2. 路由到 entity（user/group）                         │
   │  3. 两级去重（SHA-256 → FTS5+LLM）                      │
   │  4. 写入 TOML + SQLite 索引                             │
   │  5. 反思升维 → reflections/                             │
   │  6. 更新 profile.json                                   │
   └────────────────────────────────────────────────────────┘

   ┌────────────────────────────────────────────────────────┐
   │ 快系统 — @on.llm_request inject_context                 │
   │  • 拉取当前发言者的 profile_prompt                       │
   │  • recall(query=用户最新消息, k=recall_top_k)            │
   │  • 拼成 system prompt 段 "memory"                       │
   └────────────────────────────────────────────────────────┘
```

数据存储是**双层**：
- `data/memory/entities/{type}_{quoted_id}/*.toml` — 真相源（人类可直接编辑）。`entity_id` 在落盘前会先做 URL-encoding，所以 `telegram:12345` 实际目录是 `user_telegram%3A12345/`，跨平台都安全
- `data/memory/memory_index.db` — SQLite 索引（启动时从 TOML 全量 rebuild）

### LLM 工具清单

#### `memory_add(text, entity_id?, entity_type?, importance?, tags?, memory_type?)`

写入一条新记忆。会自动做两级去重：
1. **SHA-256 哈希**：内容完全一致直接跳过
2. **FTS5 + LLM**：语义近似的会被合并为一条更短的陈述（上限 200 字，不会越合并越长）

`memory_type` 取 `fact`（默认，落在 `facts/`）或 `reflection`（落在 `reflections/`）。

#### `memory_search(query, entity_id?, entity_type?, folder?, since?, until?, tags?, k=5)`

在指定实体的长期记忆中搜索。`entity_id` 可以是逗号分隔的多个 ID，工具会**并行检索**并把结果拼起来。

- `folder`：`all`（默认）/ `facts` / `reflections` / `episodes`
- `since` / `until`：按记忆创建时间过滤，格式 `YYYY-MM-DD` 或 ISO 时间
- `tags`：要求同时包含这些标签
- 复杂问题可以换关键词/时间范围**多次调用**，由主 LLM 自己完成多步回忆（KiraOS 不额外嵌套 Agent）

打分维度：
- FTS5 匹配分（jieba 中文分词）
- importance 加成
- 时间衰减（越久远权重越低）
- （可选）sqlite-vec 嵌入混合检索

#### `memory_update_entry(memory_id, text, entity_id?, folder?, importance?)`

覆盖一条已存在记忆的文本。`memory_id` 通过 `memory_search` 获得。

#### `memory_remove(memory_id, entity_id?, folder?)`

把记忆移入 `data/memory/archive/`。数据不会被物理删除，可手动恢复。

#### `profile_view(entity_id?)`

返回该实体画像的 LLM 友好文本（name / nickname / traits / preferences / relationships / facts）。

#### `profile_update(action, value, entity_id?, target?)`

修改画像。`action` 可选：
- `add_trait` / `remove_trait` — 维护 traits 数组
- `add_fact` — 追加 facts（高度浓缩的关键信息）
- `set_name` — 设置 profile.name
- `set_relationship` — 设置关系；`target` 是关系的另一方（必填）

### 上下文自动注入

每次 LLM 调用前 `@on.llm_request` 钩子会注入到 `system_prompt` 的 `memory` 段：

1. **画像段** — 当前消息中所有发言者的用户画像（受 `inject_profile` 控制）；群聊时还会注入当前群的群画像（受 `inject_group_memory` 控制）
2. **召回段** — 基于“最新一条用户消息”做 top-K 检索：
   - 主发言者的 user facts/reflections（受 `recall_top_k` 控制）
   - 当前群的 group facts/reflections（受 `inject_group_memory` 控制）
   - `global/facts` 的全局知识（受 `inject_global_facts` 控制，如导入的黑话）
   - fact / reflection 可分别用 `inject_facts` / `inject_reflections` 开关；召回段总量受 `max_recall_chars` 限制，预算在 user/group/global 之间均分，避免某一类挤掉其它类

末尾还会追加一段“主动记忆提示”，鼓励 LLM 看到事实就调用 `memory_add` 记录。

### 跨 adapter 身份关联

同一个人可能通过 QQ、WebChat、Telegram、Discord 等多个 adapter 和 bot 聊天。KiraOS 默认按 `{adapter}:{platform_id}` 隔离实体，于是每个账号会得到独立画像和记忆。`identity_aliases` 可以把这些账号映射到同一个 canonical 用户实体：

```json
{
  "identity_aliases": [
    "webchat:admin=qq:1000000001",
    "telegram:123456=qq:1000000001"
  ]
}
```

格式是一行一个 `alias=canonical`，两边都必须是完整的 `adapter:id`。只影响 **user** 实体；群/频道不参与。

启用后：

- 所有读写（画像、facts、recall、工具、WebUI、海马体路由）都会先解析到 canonical 用户，新记忆不会再分裂到 alias 实体。
- 启动时会把 `entities/user_{alias}/` 里已有的画像和 TOML 合并进 canonical：facts/traits/aliases 取并集，interaction_count 相加，last_interaction 取最大；同文本记忆去重，同文件名不同文本会加内容 hash 后缀，不覆盖。
- 合并是幂等的：alias 目录处理后会被删除，下次启动无事可做。
- 支持链式映射（`telegram:42=webchat:admin`，`webchat:admin=qq:...`）和循环检测；非法/循环条目会在日志里告警并跳过。
- 只做合并、不做反向拆分：把已有数据改成 alias 映射容易，撤掉映射不会把 canonical 里的数据自动还回去。

典型用途：给内置 WebChat 配 `"webchat:admin=qq:<你的QQ号>"`，这样在本体 WebChat 里聊天时，bot 会自动使用你 QQ 账号的画像和记忆，而不是空的 `webchat:admin` 画像。

### 事件记忆（Episode）

MaiBot 的 `chat_history` 层是“事件记忆”：把一段群聊切成话题，总结成主题/概括/关键词/关键信息点，之后用 ReAct Agent 检索。KiraOS 的 facts/reflections 偏原子事实，会丢掉“这段时间群里发生了什么”的叙事，所以新增了 episode 层：

- `on.im_message` 记录**每一条**会话消息（不只触发 bot 回复的那条），`on.step_result` 记录 bot 的回复；群聊/私聊都缓冲。
- 缓冲达到 `episode_threshold` 条，或最旧消息超过 `episode_window_minutes` 分钟时，用提取 LLM 总结成一条事件：
  - `theme`（主题）、`summary`（概括）、`keywords`、`participants`
- 落盘到 `entities/{user|group}_{id}/episodes/episode_*.toml`：
  `type = "episode"`, `importance = 6`, `tags = 关键词`, `source = 会话/时间/参与人/消息数`
- episode 和 facts/reflections 共用 FTS 索引与 recall；`inject_episodes` 控制是否注入，`memory_search(folder="episodes")` 可直接检索。
- `episode_require_bot=true`（默认）时，窗口里没有 bot 发言就丢弃，避免为每个群都付费总结。
- 缓冲写入 `data/memory/.episode_buffers`，重启后继续；没有 LLM 时保留缓冲等下次。

### 运行状态与最近回忆

这两项对应 MaiBot 的“工作缓存 + thinking_back 答案复用”：

- `.hippocampus_pending`：尚未达到 `hippocampus_threshold`、或失败待重试的对话块，重启后恢复，避免每个会话丢最多几轮上下文。
- `.recall_cache`：每个会话最近一次成功召回的内容，在 `recall_cache_ttl`（默认 600 秒）内作为 `[最近回忆]` 注入下一轮，避免新消息一换就“忘记刚才聊过什么”。
- 两者都是隐藏文件，不带 `.json` 后缀，启动重建索引时不会被当成旧 JSON 记忆扫描。

### 海马体（慢系统）

`@on.step_result` 在每次 LLM 完成响应后构造一个对话块：

```python
chunk = [
    {"role": "user", "content": user_text, "sender_id": uid, "sender_name": nick},
    {"role": "assistant", "content": assistant_text},
]
```

并调用 `memory_manager.update_memory(session_id, chunk)`。`update_memory` 把 chunk 追加到短期窗口（`max_memory_length`），同时把 chunk 推进海马体缓冲。当缓冲累积到 `hippocampus_threshold` 条时，**异步任务**触发：

1. `MemoryExtractor.extract_personal_facts` / `extract_group_facts` 调 LLM 提取事实
2. 程序化路由：根据 `sender_map` 决定每条事实归到哪个 entity
3. `deduplicate_and_store` 做两级去重后落 TOML + 索引
4. `check_elevation_trigger` 判断是否需要把零散 facts 升维成 reflection
5. `_update_profile_from_facts` 把 importance≥7 的事实写入 entity profile

整个过程跑在后台 asyncio task，**不阻塞**对话主流程。插件 `terminate()` 时会等待所有 in-flight 海马体任务完成（30s 超时）。

### TOML 真相源

每条记忆是一个 TOML 文件，结构很简单：

```toml
id = "loves_python"
type = "fact"
text = "用户喜欢 Python，特别欣赏其类型注解"
importance = 7
tags = ["programming", "preference"]

[source]
session = "telegram:dm:12345"
time = 2026-05-13T03:21:08+08:00
```

直接编辑这些文件即可修改记忆。`MemoryIndex.rebuild_index_from_files()` 会在启动时把所有 TOML 全量重扫一遍，所以即使删了整个 `memory_index.db` 也能从 TOML 还原索引。

---

## Memory WebUI

可视化管理界面。配置 `webui_port > 0` 启用：

```yaml
webui_port: 8765
webui_host: "127.0.0.1"
webui_token: "your-secret-token"  # 可选；为空表示无认证
```

访问 `http://127.0.0.1:8765/`。如果配置了 `webui_token`，**不要**把 token 放进 URL 的 query string——即使前端会自动把它移除，token 也已经进过浏览器历史、代理 access log 和截图分享渠道了。推荐做法：
- 通过 URL fragment 传入（不会发到服务器、不会进 Referer）：访问 `http://127.0.0.1:8765/#token=your-secret-token`，SPA 读取后立刻 `history.replaceState` 把 fragment 擦掉
- 或者直接在浏览器开发者工具里 `sessionStorage.setItem("kira_token", "your-secret-token")` 然后刷新

### REST API

所有 API 需要 `Authorization: Bearer <token>` 头（除非未设 token）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/stats` | 实体/记忆数量统计 |
| GET | `/api/entities[?type=user]` | 实体列表 |
| GET | `/api/entity/{type}/{id}` | 实体详情（profile + facts + reflections） |
| PUT | `/api/entity/{type}/{id}/profile` | 修改画像字段（name/nickname/platform/description） |
| POST | `/api/entity/{type}/{id}/facts` | 添加 fact |
| DELETE | `/api/entity/{type}/{id}` | 删除整个实体（移到 archive） |
| PUT | `/api/memory/{type}/{id}/{folder}/{memory_id}` | 修改单条记忆 |
| DELETE | `/api/memory/{type}/{id}/{folder}/{memory_id}` | 归档单条记忆 |
| GET | `/api/search?q=...&entity_id=...&k=10` | 全文检索 |

---

## 技能路由系统

技能路由部分与 v2.x 完全相同，只摘要要点。详细规范请参考 [skills/](skills/) 下的示例。

- 每个技能是 `data/skills/<name>/` 下的一个文件夹
- 推荐使用单文件 `SKILL.md`（YAML frontmatter + Markdown body）
- 兼容传统的 `manifest.json` + `instruction.md` 双文件格式
- 启动时插件扫描发现所有技能，每个技能注册为一个 LLM 工具
- LLM 调用工具时，instruction body 作为 tool_result 同轮返回（零额外 LLM 调用）
- 第三层渐进披露：技能可附带 `references/` / `resources/` / `scripts/` / `data/` 目录，LLM 按需通过 `read_skill_resource(skill_name, path)` 工具读取

通过 `disabled_skills` 配置项可以禁用特定技能。`enable_slash_commands: true` 允许用户用 `/<command>` 触发技能。

---

## 配置项

| Key | 类型 | 默认 | 说明 |
|-----|------|------|------|
| `hippocampus_threshold` | int | 3 | 累积多少 chunk 触发一次海马体后台运行 |
| `recall_top_k` | int | 5 | 每轮注入的记忆条数 |
| `max_memory_length` | int | 20 | 短期对话历史窗口大小（chunk 数） |
| `inject_profile` | bool | true | 是否每轮注入用户画像 |
| `inject_facts` | bool | true | 是否每轮注入召回的 facts |
| `inject_reflections` | bool | true | 是否每轮注入召回的 reflections |
| `inject_group_memory` | bool | true | 群聊时是否注入群画像与该群 top-K 记忆 |
| `inject_global_facts` | bool | true | 是否每轮注入 global/facts 的 top-K 全局知识 |
| `max_recall_chars` | int | 1500 | 召回段总字符预算（user/group/global 均分） |
| `identity_aliases` | list | `[]` | 跨 adapter 用户映射，格式 `alias=canonical`，如 `webchat:admin=qq:123` |
| `enable_episode_memory` | bool | true | 事件记忆：缓冲整段会话并总结成 episode |
| `episode_threshold` | int | 40 | 缓冲多少条消息触发事件总结 |
| `episode_window_minutes` | int | 30 | 缓冲最旧消息超过多少分钟也触发 |
| `episode_require_bot` | bool | true | 只总结 bot 参与过的窗口 |
| `inject_episodes` | bool | true | 召回/注入 episodes 目录 |
| `persist_hippocampus_pending` | bool | true | 持久化海马体待处理队列 |
| `inject_recent_recall` | bool | true | 注入最近一次召回（[最近回忆]） |
| `recall_cache_ttl` | int | 600 | 最近回忆缓存时长（秒，0=关闭） |
| `force_rebuild_index` | bool | false | 每次启动强制全量重建索引（平时只在索引与 TOML 数量不一致时重建） |
| `enable_decay` | bool | true | 启用记忆衰减（手动调用，未来加调度） |
| `auto_migrate_legacy_db` | bool | true | 启动时自动迁移 v2 kiraos.db |
| `skills_dir` | string | `data/skills/` | 技能目录 |
| `disabled_skills` | list | `[]` | 禁用的技能名列表 |
| `enable_slash_commands` | bool | false | 允许 `/cmd` 触发技能 |
| `webui_port` | int | 0 | WebUI 端口（0 = 禁用） |
| `webui_host` | string | `127.0.0.1` | WebUI 绑定地址 |
| `webui_token` | string | `""` | WebUI Bearer token |

---

## 数据存储

```
data/memory/
├── memory_index.db                      # SQLite 索引（可重建）
├── chat_memory.json                     # 短期对话历史
├── .migrated_v3                         # 迁移标记（v2 → v3 一次性迁移）
├── .qanmocore_migrated                  # MaiBot 导入 manifest（隐藏，无 .json）
├── .hippocampus_pending                 # 未处理/失败重试的海马体 chunk（隐藏）
├── .episode_buffers                     # 事件记忆的会话缓冲（隐藏）
├── .recall_cache                        # 最近召回缓存（隐藏）
├── entities/
│   ├── user_{quoted_id}/                # 例: user_telegram%3A12345 (URL-encoded)
│   │   ├── profile.json                 # 用户画像
│   │   ├── facts/{id}.toml              # 细粒度事实
│   │   ├── reflections/{id}.toml        # 高阶洞察
│   │   └── episodes/{id}.toml           # 事件记忆（type=episode）
│   ├── group_{quoted_id}/               # 群 ID 同样 URL-encoded
│   │   ├── profile.json
│   │   ├── facts/
│   │   ├── reflections/
│   │   └── episodes/
│   └── channel_{quoted_id}/...
├── global/
│   ├── facts/
│   ├── self/                            # AI 自我觉察（Phase 1）
│   │   ├── facts/
│   │   └── reflections/
│   └── skills/
└── archive/                             # 衰减/手动删除后归档
```

`data/skills/` 的结构与 v2.x 一致，详见示例技能。

---

## 从 v2 迁移

v3 第一次启动时（`auto_migrate_legacy_db: true`，默认开），插件会自动：

1. 检测 `data/memory/kiraos.db` 是否存在且有 v2 表
2. 把 `user_profiles` 行转换为 entity profile（`entities/user_{uid}/profile.json`）
   - `name` / `nickname` / `platform` / `description` 等熟悉的 key 自动填入对应字段
   - `category=basic` → traits
   - `category=preference` → preferences dict
   - `category=social` → relationships dict
   - `category=other` 或未知 → facts 列表
   - 过期条目（`expires_at < now`）跳过
3. 把 `event_logs` 行转换为 `entities/user_{uid}/facts/event_{id}.toml`
4. 把旧库重命名为 `kiraos.db.bak_<timestamp>`
5. 落盘 `.migrated_v3` 标记，保证二次启动不重复迁移

迁移逻辑在 [memory/migrations.py](memory/migrations.py)。如果想跳过，把 `auto_migrate_legacy_db` 设为 `false`。

**v2 → v3 API 变化：**
- ❌ 旧工具 `memory_update` / `memory_query` / `memory_clear` / `consolidate_memory` 已全部移除
- ✅ 新工具 `memory_add` / `memory_search` / `memory_update_entry` / `memory_remove` / `profile_view` / `profile_update`
- 注入到 system prompt 的 `memory` 段格式不同，但 LLM 一般不需要关心结构

## 从 Hippocampus Memory 插件迁移

> **v4.0.0 新增**：统一了原独立的 `kira_plugin_hippocampus_memory`（海马体记忆插件）所有功能。

每次启动时（`auto_migrate_legacy_db: true`，默认开），插件会对账一次 `data/plugin_data/kira_plugin_hippocampus_memory/memory/`，只处理新增或有变化的文件：

1. **记忆**：`entities/*/{facts,reflections,skills}`、`global/facts`、`global/self/*`、`global/skills/*`，以及旧版 JSON 记忆，按原命名空间写入 KiraOS
   - 运行时 meta（`timestamp` / `last_accessed` / `access_count`）从旧插件的 `memory_index.db`（只读打开）、TOML 内嵌 meta 和 `source.time` 恢复，取最早的创建时间、最大的计数
   - KiraOS 里已有的副本（包括 4.0.x 导入的）原地修复 meta，不会重复导入；同 id 不同内容的记忆以 `{id}_{hash8}` 共存
   - 新导入的记忆带 `hippocampus-import` 标签
2. **画像** (`profile.json`)：union merge，traits / facts / aliases 去重合并，interaction_count 取最大，冲突字段以 `last_interaction` 较新的一方为准
3. **归档**：旧插件 `archive/` 增量复制到 `data/memory/archive/hippocampus/`，不覆盖已有文件
4. 首次成功后在 plugin manager 里禁用 `kira_plugin_hippocampus_memory`（防止双喂），只禁用一次。之后你手动重新启用不会被再次关掉，它新写的记忆会在下次启动时并入
5. 处理记录写进 `data/memory/.hippocampus_migrated`（v2 manifest，逐文件记录状态）

**不会被导回来的东西**：迁移后在 KiraOS 里删除或归档的记忆、在 WebUI 里删除的实体（实体级记录在 manifest 里，清空 `archive/` 后也不会复活）。即使旧插件那边的文件后来变了，也不会导回来。KiraOS 里改过的记忆，之后旧插件那边再怎么改都以 KiraOS 的文本为准。

例外：从 4.0.x 升级时，旧 marker 没有逐文件记录。如果你在 4.0.x 期间**手动删掉**了 `data/memory/` 下某条导入记忆的 TOML 文件（而不是用 WebUI / `memory_remove`，它们走归档），升级后它会被重新导入。

**从 4.0.0 / 4.0.1-beta.1 升级**：旧版的一次性迁移漏掉了 `global/self`、skills、跨实体同文本的记忆，很多记忆的 meta 也被重置成了"刚创建"。升级后第一次启动会自动补齐并修复，不需要手动操作。

**迁移后 Hippocampus 插件会发生什么？**
- 旧数据保留在 `data/plugin_data/kira_plugin_hippocampus_memory/` 不变（只读，rollback 用）
- 插件在当前 KiraAI 实例里被自动禁用（可在 WebUI 插件管理里看到）
- 如果一条记忆都没能导入（写入失败或源文件全部无法解析），旧插件保持启用、不写 manifest，下次启动重试
- manifest 损坏或版本不认识时，本次启动跳过对账并在日志里报错，manifest 原样保留，不会被空的覆盖掉

**Hippocampus 独有功能的迁移状态**：
- ✅ **TOML + SQLite 双存储** — 完全一致
- ✅ **FTS5 全文检索** + optional `sqlite-vec` — 完全一致
- ✅ **海马体后台提取** + SHA-256 / FTS5 / LLM 三级去重 — 完全一致
- ✅ **升维反思** (dimensional reflection) — 完全一致
- ✅ **实体画像** (user/group/channel) + alias 追踪 — 完全一致
- ✅ **衰减遗忘** — 完全一致
- ✅ **三级人设演进** (persona evolution) — 完全一致，Tier 3 默认关闭
- ✅ **Group-chat 双路由** — 完全一致
- ✅ **`jieba` 中文分词** + 优雅降级 — 完全一致

迁移逻辑在 [memory/migration_hippocampus.py](memory/migration_hippocampus.py)。如果不希望自动迁移，把 `auto_migrate_legacy_db` 设为 `false`。

**如何确认迁移成功？**

启动日志里搜 `Hippocampus reconcile`，会列出本次导入 / 更新 / 跳过的数量。逐文件状态在 manifest 里：
```bash
cat data/memory/.hippocampus_migrated
```
文件内容示例：
```json
{
  "version": 2,
  "since": 1760774400.0,
  "plugin_disabled": true,
  "processed": {
    "entities/user_telegram%3A12345/facts/hates_css.toml": {
      "state": "imported", "hash": "…", "id": "hates_css", "text_hash": "…"
    }
  },
  "profiles": {
    "entities/user_telegram%3A12345/profile.json": {"hash": "…"}
  }
}
```

迁移后可通过 Memory WebUI 或 `memory_search` 工具确认数据已就位。原 Hippocampus 插件目录可在确认无误后手动删除。

---

## 从 MaiBot 迁移记忆

> 这是面向 **MaiBot（旧 QQ bot）** 数据的一次性导入工具，与上面的 Hippocampus 插件迁移互不相干。迁移只读旧库、不调用 LLM、不联网，直接把长期记忆写成 KiraOS 的 TOML 真相源；KiraAI 下次启动时自动重建 SQLite 索引。

### 迁移什么

| 旧数据 | 目标 | 说明 |
|------|------|------|
| `person_info` | `entities/user_{adapter}:{qq}/` | 昵称、群昵称、画像 facts（top-N） |
| `person_info.memory_points` | 同用户 `facts/` | 逐条细粒度事实 |
| `chat_streams` 群 | `entities/group_{adapter}:{group}/` | 群名字/别名/最近活跃 |
| `chat_history` | 群 `reflections/` | 主题 + 摘要 + 关键词，原文不导 |
| `memory_chest` | 群或用户 `facts/` | 记忆宝盒知识三联 |
| `jargon`（有释义） | `global/facts/` | 黑话/群文化知识 |
| `thinking_back`（有可用答案） | 群或用户 `facts/` | “X 是什么/谁”的问答缓存 |
| `memory_conflicts`（有答案） | 群或用户 `facts/` | 冲突澄清结论 |
| `memory` 表（非空时） | 群或用户 `facts/` | 旧版残留表 |

不迁移：`expression`（表达方式）、`messages`（原始消息）、emoji/图片、`hippo_memorizer` 话题缓存（可用 `--include-hippo-topics` 额外导入）。所有导入记忆带 `qanmocore-import` 标签。

### 使用

先停掉旧 MaiBot 和 KiraAI，然后在 KiraAI 仓库根目录执行（Linux/macOS）：

```bash
# 1) 先看会导多少条，不写任何目标文件
python data/plugins/KiraOS_Plugin/scripts/import_qanmocore.py \
    --source /path/to/MaiBot/data \
    --data-dir data \
    --dry-run

# 2) 确认 adapter 名称后正式导入
python data/plugins/KiraOS_Plugin/scripts/import_qanmocore.py \
    --source /path/to/MaiBot/data \
    --data-dir data \
    --adapter qq
```

Windows 用 `data\plugins\KiraOS_Plugin\scripts\import_qanmocore.py`，并把 `--data-dir` 写成 `data`。

**实体 ID 前缀必须与 KiraAI 里 QQ adapter 的实例名一致**。脚本会读 `data/config/system_config.json`，如果只有一个 platform=qq 的 adapter 就自动用它；否则默认 `qq`，请用 `--adapter` 指定。前缀不对会导致画像/记忆写进另一个命名空间、对话里读不到。

### 常用参数

| 参数 | 作用 |
|------|------|
| `--dry-run` | 只统计，不写目标 |
| `--include` / `--exclude` | 选择类别：`profiles,history,chest,jargon,qa,conflicts,memory,hippo-topics` |
| `--profile-facts N` | 每个画像最多复制多少条 facts（默认 12） |
| `--since YYYY-MM-DD` / `--until` | 按源时间过滤 chat_history / QA / conflicts |
| `--max-per-chat N` | 每个群最多导多少条摘要/问答（按时间倒序取最近） |
| `--keep-unanswered` | 保留“未找到/无法确认”类问答 |
| `--no-backup` | 目标已有记忆时不先备份 `data/memory` |
| `--rebuild-index` | 导入后立即重建索引（默认留给 KiraAI 启动时做） |
| `--allow-live-source` | 旧库 `-wal` 非空时仍只读导入 |
| `--fail-on-error` | 有单条失败时以非 0 退出 |

### 幂等与安全

- 重复运行不会产生重复记忆：源数据没变的条目直接跳过；源数据更新时原位更新。
- 你在 KiraOS 里删除/归档的导入记忆不会被重新复活；`--force` 可重新导入已标记删除的条目。
- 目标里已有同 id 但**没有** `qanmocore-import` 标签的记忆视为你自己的数据，绝不覆盖。
- 你在 KiraOS 里手改过的导入记忆，后续源数据变更不会覆盖它。
- 旧库以只读 `immutable` 打开，不会在 MaiBot 目录里留下 `-wal`/`-shm` 文件。
- 逐条状态记录在 `data/memory/.qanmocore_migrated`（隐藏文件，故意不带 .json 后缀，避免被当成旧 JSON 记忆扫描）；导入统计另存到 `data/migration_reports/`。

### 迁移后

1. 启动 KiraAI。KiraOS 启动时会检查索引与 TOML 数量：一致就直接复用现有索引（2.3 万条实测约 0.2 秒），不一致才重建。正常启动不需要等一分钟。
2. 打开 Memory WebUI（如配置了 `webui_port`）或直接问 bot “你记得 XX 吗”，确认画像/群摘要/黑话能召回。
   - KiraAI 自带 WebChat，不配 QQ adapter 也能在 WebUI 本体里聊天（会话实体固定为 `webchat:admin`）。
   - 但导入的 QQ 记忆按 `qq:...` 隔离：WebChat 里会自动注入 global 知识（如黑话），**不会自动**注入某个 QQ 用户的画像/群记忆。要在 WebChat 里验证旧 QQ 记忆，让 bot 显式调用工具：
     - `memory_search(entity_id="qq:1000000002", entity_type="user")`
     - `memory_search(entity_id="qq:1000000003", entity_type="group")`
     - `memory_search(entity_type="global")`
     - `profile_view(entity_id="qq:1000000002")`
   - 如果希望 WebChat 会话自动使用某个 QQ 用户/群的记忆，需要额外的别名配置（当前未内置，可按需扩展）。
3. 迁移保留旧 bot 名字（如“浅陌”）在记忆文本里；如果 KiraAI 人设换名，可在 persona 配置里使用同名，或后续用 `--since` 重新挑选。
4. 确认无误后，旧 `MaiBot/data` 可作为 rollback 保留，不必删除。

> 迁移只覆盖长期记忆。旧 bot 的短期上下文、聊天原文、表达方式学习结果不会进入 KiraAI。

---

## 故障排除

### 启动报 `No module named 'tomli_w'` / `No module named 'jieba'` 等
依赖没装。最稳妥的做法是按上面 [依赖](#依赖) 一节执行：
```bash
python -m pip install -r data/plugins/KiraOS_Plugin/requirements.txt
```
WebUI 装的插件会自动跑，本地直接 clone 的需要手动跑一次。`jieba` 缺失插件仍能加载（自动降级），但 `tomli_w` 缺失会让插件加载失败。

### 海马体不触发
检查：
1. `hippocampus_threshold` 配置（默认 3 条 chunk 才触发一次）
2. 日志里有没有 `Hippocampus completed for session...`
3. `memory_manager._llm_client` 是否注入成功（看 initialize 日志）

### WebUI 显示 `stats failed`
检查端口是否被占用；token 是否匹配；浏览器控制台 fetch 报错的具体 HTTP 状态码。

### 想完整重建索引
平时启动**不会**全量重建：只有索引行数与 TOML 文件数不一致（或索引缺失/损坏）才重建，所以 2 万条记忆也不会拖慢每次启动。手动改过 TOML 文本后，可以临时把 `force_rebuild_index` 设为 `true` 启动一次，再关掉；或删掉 `data/memory/memory_index.db` 重启。TOML 文件是真相源。

### 想完整重置记忆
关闭插件，删整个 `data/memory/`，重启。所有数据丢失（包括 v2 备份），慎用。
