# KiraOS 插件

> **插件 ID**: `kira_plugin_kiraos`
> **版本**: 3.0.0
> **作者**: LyaQanYi

KiraOS 是 KiraAI 的 OS 级插件，整合了两大核心能力：

| 能力 | 类比 | 说明 |
|------|------|------|
| **记忆系统 (Memory)** | 长期记忆中枢 | 双脑架构：TOML 文件 + SQLite 索引、FTS5 + 可选向量检索、海马体提取/反思/升维、衰减 GC、多实体画像 |
| **技能路由 (Skill Router)** | 程序加载器 | 渐进式工具发现——启动时加载轻量 manifest，运行时按需注入完整指令 |

3.0.0 版本对记忆子系统做了完整重构，照搬 KiraAI-lightning 的双脑架构（TOML 真相源 + SQLite 索引 + 海马体后台提取 + 衰减 GC）。旧的 `user_profiles + event_logs` SQLite KV 模型已废弃，首次启动会自动迁移。

---

## 目录

- [架构总览](#架构总览)
- [LLM 工具一览](#llm-工具一览)
- [配置项](#配置项)
- [启动流程](#启动流程)
- [数据迁移](#数据迁移)
- [记忆存储布局](#记忆存储布局)
- [海马体后台流程](#海马体后台流程)
- [WebUI](#webui)
- [技能路由](#技能路由)
- [开发与测试](#开发与测试)

---

## 架构总览

```
KiraOS_Plugin/
├── memory/                  # 记忆子系统（从 KiraAI-lightning 移植）
│   ├── memory_paths.py      # data/memory/ 目录布局 + ID 校验
│   ├── memory_index.py      # SQLite + FTS5 + 可选 sqlite-vec
│   ├── toml_tree_store.py   # TOML 内容文件 + 索引同步
│   ├── entity_profile.py    # 实体画像（user/group/channel）
│   ├── memory_extractor.py  # 海马体：提取 → 去重 → 合并 → 升维反思
│   ├── memory_decay.py      # 保留分数 + 垃圾回收
│   └── memory_manager.py    # 瘦身版门面：recall / process_turn / get_profile
├── tools/
│   └── memory_tools.py      # 6 个 LLM 工具的纯逻辑实现 + 智能 entity 解析
├── skills/                  # 技能定义（SKILL.md 格式）
├── skill_router.py          # 技能发现 / 装载 / 资源读取
├── web/index.html           # WebUI 单页前端
├── web_server.py            # Starlette REST API
├── main.py                  # 插件入口：lifecycle + tool 注册 + Hook
├── migrate.py               # 旧 kiraos.db → 新 TOML 一次性迁移
├── manifest.json
└── schema.json
```

**记忆双层存储**：

| 层 | 角色 |
|---|---|
| TOML 文件（`data/memory/entities/<type>_<id>/<folder>/*.toml`） | 真相源、人类可读、可手动编辑、可版本控制 |
| SQLite (`data/memory/memory_index.db`) | 运行时索引、FTS5 全文检索、（可选）向量、`access_count`/`last_accessed`/`importance` 等元数据 |

启动时会自动从 TOML 文件全量重建 SQLite 索引，所以即使删了 `memory_index.db` 也能完整恢复。

---

## LLM 工具一览

所有工具都自动注册到 `ctx.llm_api`，可被主 LLM 调用。

| Tool | 作用 | 必填参数 |
|---|---|---|
| `memory_add` | 写入一条记忆，走 SHA-256 + FTS5 + LLM 三级去重；重复时合并 | `text` |
| `memory_update` | 编辑已有记忆的 text / importance | `memory_id`, `text` |
| `memory_remove` | 归档记忆（移到 `archive/`，可恢复） | `memory_id` |
| `memory_search` | 语义搜索；支持逗号分隔多 entity 并行；缺省 `entity_id` 时 fast LLM 自动从对话上下文提取 | `query` |
| `profile_view` | 展示实体画像（name/nickname/aliases/traits/preferences/relationships/facts） | — |
| `profile_update` | 修改画像：`add_trait` / `remove_trait` / `add_fact` / `set_name` / `set_relationship` | `action`, `value` |

**entity_id 解析（五段式）**：

1. LLM 误传群号特征字符串 → 拦截并丢弃
2. 看起来是 `adapter:numeric_id` → 直接使用
3. 看起来是昵称 → 通过 profile 反查 (`name`/`nickname`/`aliases`)
4. 缺省时 fast LLM 从对话上下文提取
5. 全部失败 → 当前发言者兜底

旧版的 `memory_update KV 模式 / memory_query / consolidate_memory / memory_clear` 已**移除**，没有 alias 保留。

---

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `memory_top_k` | 5 | 每轮注入 LLM 上下文的记忆条数 |
| `memory_inject_max_chars` | 800 | 记忆 + 画像注入块的字符上限（0 = 不限） |
| `enable_vector_search` | false | 启用 sqlite-vec 混合检索；未装扩展时自动回退 FTS5 |
| `vector_dim` | 768 | 向量维度（启用向量时需与 embedder 匹配） |
| `decay_enabled` | true | 启用记忆衰减 |
| `decay_interval_days` | 14 | 完整遗忘周期间隔 |
| `gc_importance_threshold` | 3 | importance ≤ 此值且久未访问 → 归档 |
| `gc_unaccessed_days` | 30 | 未访问超过此天数视为候选 GC |
| `hippocampus_enabled` | true | 启用后台海马体提取（费 token，但记忆更全） |
| `hippocampus_model_uuid` | "" | 海马体使用的 LLM；空 → fast LLM |
| `hippocampus_max_inflight` | 4 | 并发上限（1-32） |
| `hippocampus_skip_keywords` | (8 个中文短语) | 用户消息含这些词时跳过海马体 |
| `skills_dir` | `data/skills` | 技能扫描根目录 |
| `disabled_skills` | `[]` | 禁用的技能名列表 |
| `enable_slash_commands` | false | 允许 `/cmd` 触发技能 |
| `webui_port` | 0 (禁用) | WebUI 端口（1-65535） |
| `webui_host` | `127.0.0.1` | WebUI 绑定地址 |
| `webui_token` | "" | WebUI Bearer 鉴权 |

---

## 启动流程

`initialize()` 顺序：

1. 检测并禁用内置 `kira_plugin_simple_memory`（避免冲突）
2. `ensure_directory_structure()`：创建 `data/memory/{entities, global, archive, ...}`
3. 实例化 `MemoryIndex / TomlTreeStore / EntityProfileStore / MemoryExtractor / MemoryDecayEngine`，组装到 `MemoryManager`
4. 从 TOML 文件全量重建 SQLite 索引（`async_init`）
5. **一次性迁移旧 kiraos.db**（若存在）：见下节
6. 注入 `tools/memory_tools.py` 的全局引用
7. 注册 6 个 memory tool（`@register_tool` 装饰器）
8. 扫描并注册技能（保留 v2.0 行为）
9. 启动 WebUI（如果 `webui_port > 0`）

`terminate()` 顺序：

1. Drain 后台海马体任务（最多 5s 等待，超时则 cancel + 再 2s 等待，避免与 DB 关闭竞态）
2. 关闭 WebUI
3. 注销 skill tools、resource tool
4. 清空 `tools/memory_tools.py` 全局引用
5. 关闭 SQLite 索引连接

---

## 数据迁移

旧版（v2.x）使用 `data/memory/kiraos.db`（或更早的 `user_memory.db`）作为单文件 SQLite，表结构为 `user_profiles(user_id, memory_key, memory_value, ...)` + `event_logs(...)`。

新版第一次启动时，`migrate.migrate_legacy_db_if_needed` 会：

1. 检查 `data/memory/.migrated_v3` 哨兵文件 → 已迁移则跳过
2. 检查旧 db 路径（依次试 `kiraos.db` / `user_memory.db`） → 不存在则写哨兵后跳过
3. 逐行转 TOML：
   - **每条 user_profiles 行** → `entities/user_<user_id>/facts/<slug>.toml`
     - `text` = `"{key}: {value}"`
     - `importance` = category 基线 (basic=8, preference=6, social=5, other=4) + round(confidence×2)，封顶 1-10
     - `tags` = `[category]` (+`"ttl"` 若有 expires_at) + `"legacy_profile"`
     - `semantic_id` = slugified key；冲突时加 `_1`、`_2` 后缀
     - `source.legacy` 保留原始字段以备追溯
   - **每条 event_logs 行** → `entities/user_<user_id>/facts/event_<id>_<yyyymmdd>.toml`
     - `text` = event_summary
     - `tags` = `[tag, "event", "legacy_event"]`（无 tag 时 `["event", "legacy_event"]`）
4. 成功后将旧 db 重命名为 `kiraos.db.legacy.bak`（保留备份）并写入哨兵

如需手动重跑：删 `data/memory/.migrated_v3` 即可。

---

## 记忆存储布局

```
data/memory/
├── entities/
│   ├── user_<entity_id>/
│   │   ├── profile.json           # EntityProfile（name, nickname, traits, facts, ...）
│   │   ├── facts/                 # 单条事实
│   │   │   ├── <slug>.toml
│   │   │   └── ...
│   │   └── reflections/           # 高层洞察（facts 升维后的产物）
│   │       └── <slug>.toml
│   ├── group_<group_id>/...
│   └── channel_<channel_id>/...
├── global/
│   ├── facts/                     # 全局通用事实（不属于任何实体）
│   ├── skills/                    # 全局技能库
│   └── self/                      # AI 自身行为觉察
│       ├── facts/
│       └── reflections/
├── archive/                       # 归档（软删除），含完整 meta 可恢复
│   └── <slug>.toml
└── memory_index.db                # SQLite 索引（FTS5 + 可选 sqlite-vec）
```

每个 TOML 文件的最小 schema：

```toml
id = "likes_python"
type = "fact"               # 或 "reflection"
text = "用户喜欢 Python 编程"
importance = 7              # 1-10
tags = ["programming", "python"]

[source]
time = "2026-03-01T14:30:00+08:00"
```

运行时元数据（`access_count` / `last_accessed` / `timestamp`）只存 SQLite，不写入 TOML——保证文件可手工编辑而不破坏运行时状态。

---

## 海马体后台流程

主 LLM 完成本轮回复后，`@on.step_result` 钩子 **fire-and-forget** 调度一次海马体：

1. **过滤**：skip 关键词命中 / 用户文本过短 / 并发上限 → 跳过
2. `MemoryExtractor.extract_personal_facts(conversation_text)` 用快 LLM 提取本轮事实（JSON 数组）
3. 每条事实走 `deduplicate_and_store`：
   - **SHA-256 精确去重**：找到则跳过
   - **FTS5 + LLM 判断**：返回 `duplicate` / `update` / `new`
   - `update` 时 LLM 合并新旧文本后写回
4. importance ≥ 7 的事实同步追加到 `EntityProfile.facts`
5. 实体的 `facts` 数 ≥ 阈值（默认 5）→ `generate_reflections` 升维为 reflection，吸收低 importance facts

每次调用上限 30s（`asyncio.wait_for`），失败静默捕获不影响主流程。

衰减 GC 不由海马体触发；上层可周期性调用 `manager.run_forgetting_cycle()`，或通过 WebUI 的 `POST /api/gc` 按钮立即执行。

---

## WebUI

设置 `webui_port = 8765` 后浏览器访问 `http://127.0.0.1:8765`。

REST API：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/stats` | 实体数 / 记忆数 / 各 folder 计数 / 向量是否可用 |
| GET | `/api/entities?type=user\|group\|channel` | 实体列表 |
| GET | `/api/entity/{type}/{id}` | 单实体的画像 + facts + reflections |
| GET | `/api/memory/{id}` | 单条记忆的 meta + 完整 TOML 内容 |
| PUT | `/api/memory/{id}` | 编辑 `text` / `importance` / `tags` |
| DELETE | `/api/memory/{id}` | 归档 |
| POST | `/api/search` | body `{query, entity_id?, k?}` |
| POST | `/api/gc` | 立刻运行一次遗忘周期 |

鉴权：可选 `Authorization: Bearer <token>`，SPA 从一次性 `?token=` 引导后转存 `sessionStorage`。

---

## 技能路由

技能子系统与 v2.0 完全一致，**未做任何改动**。每个技能是 `data/skills/<name>/` 下的 `SKILL.md`（YAML frontmatter + 指令体）或 legacy `manifest.json + instruction.md`。LLM 触发技能时，body 作为 tool 结果返回；带资源文件的技能可通过 `read_skill_resource(skill_name, path)` 三级渐进式读取。

详见 `skill_router.py` 顶部 docstring 和 `data/skills/*/SKILL.md` 示例。

---

## 开发与测试

集成测试（11 项，含迁移测试）：

```bash
cd /Users/lyaqanyi/Documents/Coding/KiraOS-2/KiraAI
python3 -m core.plugin.builtin_plugins.KiraOS_Plugin.test_memory_system
```

预期输出每项 `OK ...` 然后 `All 11 tests passed.`。

如改了 lightning 蓝本中的逻辑，可参照 KiraAI-lightning/test_memory_system.py 补充对照测试。

依赖（除 KiraAI 本身）：

- `jieba` — FTS5 中文分词（必须）
- `tomli_w` + `tomllib`(Py3.11+) 或 `tomli`(Py3.10-) — TOML 读写
- `sqlite_vec` — 可选；启用 `enable_vector_search` 才需要
- `starlette` + `uvicorn` — 仅 WebUI 启用时需要
