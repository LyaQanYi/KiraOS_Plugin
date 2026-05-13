# KiraOS 插件文档

> **插件 ID**: `kira_plugin_kiraos`
> **版本**: 3.0.0
> **作者**: LyaQanYi
> **兼容**: KiraAI v2.x (dev branch)

KiraOS 是 Kira 的 OS 级插件，整合了两大核心能力：

| 能力 | 类比 | 说明 |
|------|------|------|
| **双脑记忆 (Dual-Brain Memory)** | 大脑 | 快/慢两套循环：FTS5 实时检索 + 后台海马体异步提取、反思、画像更新；TOML 文件作真相源，SQLite 作可重建索引 |
| **技能路由 (Skill Router)** | 程序加载器 | 渐进式工具发现——启动时加载轻量 manifest，运行时按需注入完整指令 |

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
  - [海马体（慢系统）](#海马体慢系统)
  - [TOML 真相源](#toml-真相源)
- [Memory WebUI](#memory-webui)
- [技能路由系统](#技能路由系统)
- [配置项](#配置项)
- [数据存储](#数据存储)
- [从 v2 迁移](#从-v2-迁移)
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
- `data/memory/entities/{type}_{id}/*.toml` — 真相源（人类可直接编辑）
- `data/memory/memory_index.db` — SQLite 索引（启动时从 TOML 全量 rebuild）

### LLM 工具清单

#### `memory_add(text, entity_id?, entity_type?, importance?, tags?, memory_type?)`

写入一条新记忆。会自动做两级去重：
1. **SHA-256 哈希**：内容完全一致直接跳过
2. **FTS5 + LLM**：语义近似的会被合并（旧记忆 text 被新文本扩充）

`memory_type` 取 `fact`（默认，落在 `facts/`）或 `reflection`（落在 `reflections/`）。

#### `memory_search(query, entity_id?, entity_type?, k=5)`

在指定实体的长期记忆中搜索。`entity_id` 可以是逗号分隔的多个 ID，工具会**并行检索**并把结果拼起来。

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

每次 LLM 调用前 `@on.llm_request` 钩子会注入两类内容到 `system_prompt` 的 `memory` 段：

1. **画像段** — 当前消息中所有发言者的画像（受 `inject_profile` 配置控制）
2. **召回段** — 基于"最新一条用户消息"对主发言者做 top-K 检索（受 `recall_top_k` 控制，可分别开关 fact / reflection 注入）

末尾还会追加一段"主动记忆提示"，鼓励 LLM 看到事实就调用 `memory_add` 记录。

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

访问 `http://127.0.0.1:8765/?token=your-secret-token`，token 会被 SPA 读到 sessionStorage 后从 URL 移除。

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
├── entities/
│   ├── user_{adapter}:{uid}/
│   │   ├── profile.json                 # 用户画像
│   │   ├── facts/{id}.toml              # 细粒度事实
│   │   └── reflections/{id}.toml        # 高阶洞察
│   ├── group_{gid}/
│   │   ├── profile.json
│   │   ├── facts/
│   │   └── reflections/
│   └── channel_{cid}/...
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
删掉 `data/memory/memory_index.db`，重启插件即可。TOML 文件是真相源，索引会从零重建。

### 想完整重置记忆
关闭插件，删整个 `data/memory/`，重启。所有数据丢失（包括 v2 备份），慎用。
