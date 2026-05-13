# Changelog

KiraOS_Plugin 版本历史。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [3.0.0] - 2026-05-13

完整重写的双脑记忆系统，与 v2.x 不兼容。提供从 v2 SQLite 到 v3 TOML 结构的一次性自动迁移。

### Highlights

- **🧠 双脑记忆架构**：快系统（SQLite + FTS5 + 可选 sqlite-vec）实时检索；慢系统（海马体后台异步任务）每 N 轮对话提取事实 / 反思升维 / 更新画像
- **📁 TOML 真相源**：人类可读、可手工编辑的真相源；SQLite 索引可从 TOML 全量重建
- **🆔 多实体隔离**：user / group / channel / global 四类命名空间，互不污染
- **🛡️ Defense-in-depth**：路径校验、原子写、SQLite 串行化、payload 类型守门、错误响应 sanitize
- **🌐 i18n**：manifest + schema 全字段 zh 覆盖，WebUI 自动切换显示语言
- **♻️ 自动迁移**：首次启动检测 v2 `kiraos.db` → 转换为 v3 entities/*.toml，旧库备份归档

### Added

#### 记忆系统核心
- 新增 `memory/` 子包：`memory_manager` / `memory_index` / `toml_tree_store` / `memory_extractor` / `entity_profile` / `memory_paths` / `memory_decay` / `memory_router` / `session` / `migrations` 共 10 个模块
- `MemoryIndex`：SQLite 持久化索引，复合主键 `(entity_type, entity_id, folder, base_dir, id)` + STORED 生成列 `storage_key` 支持 FTS5/vec0 JOIN
- `TomlTreeStore`：TOML 文件 CRUD + 原子写（tmp + fsync + os.replace）+ 锁串行化
- `MemoryExtractor`：海马体——事实提取 / 去重 / 合并 / 升维反思 / 自我觉察
- `EntityProfileStore`：实体画像 JSON 存储，per-entity asyncio 锁串行化读改写
- `MemoryDecayEngine`：基于 importance + last_accessed 的指数衰减 + 归档
- `MemoryManager`：双脑协调中枢，集成快慢两套循环

#### LLM 工具（6 个）
- `memory_add` — 添加记忆（两级去重：SHA-256 hash + FTS5 语义）
- `memory_search` — 跨 entity 召回（支持逗号分隔多 entity 并行）
- `memory_update_entry` — 修改已有记忆
- `memory_remove` — 移入归档（archive/ 目录，可恢复）
- `profile_view` — 查看用户画像
- `profile_update` — 更新画像（add_trait / remove_trait / add_fact / set_name / set_relationship）

#### LLM 钩子
- `@on.llm_request`：每轮注入 profile prompt + top-K 召回记忆 + 主动记忆提示
- `@on.step_result`：构造对话 chunk 喂给海马体缓冲，攒满 `hippocampus_threshold` 触发后台任务

#### 迁移与兼容
- `memory/migrations.py`：v2 `user_profiles` + `event_logs` 一次性转换到 v3 TOML，幂等（`.migrated_v3` 标记）
- 自动检测旧库 schema 不匹配时 DROP + 从 TOML rebuild SQLite 索引
- 旧 db 备份为 `kiraos.db.bak_<timestamp>`

#### Memory WebUI
- 重写 `web_server.py`（Starlette + uvicorn），10 个 REST 端点：`/api/stats`、`/api/entities`、`/api/entity/{type}/{id}`、`/api/entity/{type}/{id}/profile`（PUT）、`/api/entity/{type}/{id}/facts`（POST）、`/api/entity/{type}/{id}`（DELETE）、`/api/memory/{type}/{id}/{folder}/{memory_id}`（PUT/DELETE）、`/api/search`
- 重写 `web/index.html`：极简 SPA，左侧实体列表 + 右侧详情/编辑 + 搜索条，支持键盘导航（role/tabindex/aria-label）
- Bearer token via URL fragment（`#token=...`）+ sessionStorage，永不进入 URL query / Referer / 访问日志

#### Skill Router（保留 v2 行为）
- 完整保留 `skill_router.py` 渐进式技能发现 + on-demand 子 agent 执行
- SKILL.md (YAML frontmatter) / manifest.json + instruction.md 两种格式
- 三级渐进披露：tool 名称 → instruction body → bundled resources (`read_skill_resource`)

#### 国际化（i18n）
- `manifest.json` 加 `locales.zh.{display_name, description}` —— WebUI 插件管理页中文环境自动切换显示
- `schema.json` 14 个配置字段全部加 `locales.zh.{name, hint}`
- 默认英文 + zh 覆盖结构，与 KiraAI 内置插件协议一致

#### 配置项（v3 新增）
- `hippocampus_threshold`（默认 3）
- `recall_top_k`（默认 5）
- `max_memory_length`（默认 20）
- `inject_profile` / `inject_facts` / `inject_reflections`（默认 true）
- `enable_decay`（默认 true）
- `auto_migrate_legacy_db`（默认 true）

#### 文档与依赖
- 完整重写 `README.md` —— v3 架构图、6 个工具清单、REST API 表、配置项表、迁移指引
- 新增 `requirements.txt` —— `tomli_w` (硬依赖) + `tomli` (Py3.10 兜底) + `jieba` (软依赖，可降级)
- 新增 `CHANGELOG.md`

### Changed

- **架构层面**：v2 的单 SQLite + 双表（`user_profiles` + `event_logs`）→ v3 的 TOML 真相源 + SQLite 索引双层结构
- **API 变化**（破坏性）：
  - ❌ `memory_update` / `memory_query` / `memory_clear` / `consolidate_memory` 4 个旧工具全部移除
  - ✅ 替换为 6 个新工具（见 Added 节）
- `manifest.json` 版本号 `2.0.0` → `3.0.0`
- `manifest.json.description` 改为"OS-level plugin combining a dual-brain memory engine..."
- 默认装配位置：从 KiraAI 内置插件目录改为 **`data/plugins/KiraOS_Plugin/`**（不再随 KiraAI 主程序发布）

### Removed

- ❌ `db.py` 里的 `UserMemoryDB` 类（v2 的 SQLite 表层 ORM）
- ❌ 4 个旧 LLM 工具
- ❌ v2 配置项（`max_events_per_user` / `max_profiles_per_user` / `max_event_keep` / `inject_categories` / `max_context_chars` 等）
- ❌ v2 `memory_auditor_*` 配置（被海马体取代）

### Fixed（来自 12 轮 CodeRabbit code review）

12 轮 review 共 75 条 actionable comments，全部修复并验证通过：

**🔴 Critical（5 条）**：
- SQLite Connection 多线程不安全 → 加 `RLock` 串行化所有 SQL 访问
- `memories.id` 全局主键导致跨 entity 撞 ID → 改复合主键 + `storage_key` 生成列
- `Memory.id` / `memory_id` 路径穿越 → `_validate_memory_id` 静态校验
- Lock cap 用 `asyncio.Lock.locked()` 判断可回收 → race condition，改用 `_RefCountedLock` 引用计数包装器
- `find_by_hash` / `get_meta` / `update_meta` / `touch_access` / `delete` 漏 entity context → 全部加复合主键参数

**🟠 Major 选录**：
- 海马体异常时 chunks 不再被吞掉（exception 路径 re-buffer 回 pending 队列）
- LLM 注入后主动 drain 启动窗口积压的 hippocampus batch
- 所有 LLM 调用包 `asyncio.wait_for(timeout=30s)`
- TOML / profile.json / chat_memory.json 全走原子写（tmp + fsync + os.replace）
- per-entity asyncio 锁串行化画像读改写
- `get_profile` 读失败不再 silently 覆盖磁盘
- `update_profile` 字段级类型校验
- `memory_decay` 降级同步到 TOML 真相源，重启不会被回滚
- 向量检索补 `base_dir` 过滤维度
- `rebuild_index_from_files` 跳过 archive/ + 先 TRUNCATE 防僵尸行
- `migrate_legacy_db` 各种异常路径加 fallback（过期时间脏数据 / 缺表 / `data_root` 不存在 / 半文件覆盖防御）
- 归档文件名加 `entity_type__entity_id__folder` 命名空间防跨实体撞名
- `archive_memory` 全程加锁防与 `update_memory` 竞态
- `confirm_memory_usage` 签名从 `list[str]` 改成 `list[Memory]`（裸 ID 在复合主键下不再唯一）
- WebUI 错误响应 sanitize：所有 500 body 改成固定字符串，不暴露文件路径/栈细节
- WebUI 日志 mask entity ID（`_mask_id` = prefix + sha256[:8]）
- 全局搜索改并发 `asyncio.gather` + 保留 `meta._score` 跨 entity 排序
- payload 类型校验：`isinstance(dict)` + 字段级 `isinstance(str/int/list)`
- API 失败不再 `200 ok:false`：`update_memory` → 500，`archive_memory` → 404/500（先 `get_memory` 探测）
- SPA 实体列表加 `role="button"` / `tabindex` / `aria-label` + Enter/Space 键支持
- SPA 保存画像后并发刷新右侧详情 + 左侧侧边栏
- 海马体 reflection 输入按 `(importance, last_accessed)` 截断 Top-50 防 prompt 膨胀
- 取消"reflection 生成后自动批归档低 importance fact"的危险逻辑
- `Memory.from_toml_dict` 字段级类型 coerce（用户手改 TOML 写错类型仍可加载）
- `search()` 命中后 TOML 重读走 `Memory.from_toml_dict` 复用容错
- `self-awareness` 抽取先剥 list/markdown 前缀（`- 我...` / `1. 我...` / `**我...**`）再做"我"开头检查
- `_clean_facts` 把 `tags` 元素逐个 coerce 为非空 str + dedup（防 `set.update` unhashable）
- `_get_lock` 加 LRU cap（默认 256）+ 懒回收，防内存泄漏
- 4 个 `extract_*` 入口加 `MAX_CONVERSATION_CHARS=8000` 截断防 prompt 膨胀

**🟡 Minor 选录**：
- `memory_index.py` 多个 method 类型注解修正
- README 实体目录示例标注 URL-encoded 后的实际格式
- README 删除"WebUI token 放进 URL"的危险指引
- `profile_update` strip 空白输入
- `memory_decay` 归档失败时不再误计入删除计数
- `last_accessed` 缺省回退到 `timestamp` 防新记忆误判超久未访问

详细 PR 讨论：[#10](https://github.com/LyaQanYi/KiraOS_Plugin/pull/10)

### Security

- WebUI token 改用 URL fragment（`#token=...`）而非 query string，不再泄露到 access log / Referer / 浏览器历史
- `entity_id` / `folder` / `memory_id` 三层路径参数全部白名单校验，杜绝路径穿越
- 错误响应不暴露底层异常文本 / 文件路径 / 栈细节
- 日志中所有用户 / 群组 / 频道 ID 走 `_mask_id` 脱敏
- `find_by_hash` 加 `base_dir` 维度，防 global 命名空间内容误命中普通 entity 域

---

## 历史版本

v2.x 及以前的版本记录请参考 git history 与 GitHub Releases 页。
