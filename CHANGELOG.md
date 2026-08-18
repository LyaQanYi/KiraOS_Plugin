# Changelog

All notable changes to KiraOS Plugin will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.0.0] - 2026-08-18

### 🎯 Major: Unified Hippocampus Memory Plugin

**Breaking Change**: `kira_plugin_hippocampus_memory` is now deprecated and fully merged into this plugin.

#### Added

- **Automatic hippocampus data migration** on first launch (controlled by `auto_migrate_legacy_db`, default: `true`)
  - Detects `data/plugin_data/kira_plugin_hippocampus_memory/memory/` and migrates all entities, facts, reflections
  - Content-hash deduplication prevents duplicate imports (idempotent migration)
  - SQLite runtime meta (timestamp, last_accessed, access_count) preserved from old index
  - Entity profiles: union merge (traits/facts/aliases deduplicated, interaction_count accumulated)
  - Migration statistics logged to `data/memory/.hippocampus_migrated`
  - Auto-disables hippocampus plugin in KiraAI plugin manager after successful migration
- **LLM adapter abstraction layer** (`memory/llm_adapter.py`)
  - Unified interface for memory extractor to call LLM services
  - Decouples memory logic from KiraAI context management
  - Enables future multi-provider support
- **Migration entry point** (`memory/migration_hippocampus.py`)
  - Orchestrates hippocampus → KiraOS data pipeline
  - Integrates with existing v2 → v3 migration flow
  - **Failure-path safety**: if source memories exist but zero import successfully,
    the hippocampus plugin is left **enabled** and the marker is **not** written, so
    the next launch retries. Disabling the old plugin after a failed import would
    read to the user as total data loss.
- **Migration test suite** (`tests/test_hippocampus_migration.py`, 10 tests)
  - Builds a real hippocampus tree on disk (TOML + `profile.json` + `memory_index.db`
    using hippocampus's own schema) and asserts on what lands
  - Covers: counts, retrievability via the normal API, aged-metadata preservation,
    profile carry-over, idempotency, source-immutability, plugin auto-disable,
    corrupt-TOML resilience, clean no-op on fresh installs, and the total-failure guard

#### Changed

- **Memory extractor** now uses LLM adapter instead of direct `context.generate()` calls
- **Version bumped to 4.0.0** (major version due to plugin consolidation)
- **README** updated with hippocampus migration guide and feature matrix
- **Manifest** updated with v4.0.0 notice and hippocampus unification message

#### Migration Guide

**For existing hippocampus users:**

1. Update KiraOS Plugin to v4.0.0
2. Ensure `auto_migrate_legacy_db: true` in config (default)
3. Launch KiraAI — migration runs automatically on first start
4. Verify migration: `cat data/memory/.hippocampus_migrated`
5. Hippocampus plugin auto-disabled; original data preserved in `data/plugin_data/kira_plugin_hippocampus_memory/`

**What's migrated:**
- ✅ Entity profiles (user/group/channel)
- ✅ Facts & reflections (all folders)
- ✅ Global facts namespace
- ✅ Runtime metadata (timestamps, access counts)
- ✅ Aliases tracking

**Post-migration:**
- Original hippocampus directory remains intact (rollback insurance)
- Can safely uninstall hippocampus plugin after confirming data integrity
- All hippocampus features now available through unified KiraOS tools

---

## [3.0.1] - 2026-08-15

### Fixed

- Tool registration stability improvements

---

## [3.0.0] - 2026-08-10

### 🎯 Major: Dual-Brain Memory System

Complete rewrite of memory subsystem from v2's "two SQLite tables + single profile" to production-grade dual-brain architecture.

#### Added

- **TOML-based storage** as source of truth (human-readable, git-friendly)
- **SQLite index** (rebuildable) with FTS5 full-text search
- **Background hippocampus** for async fact extraction and reflection
- **Per-entity profiles** (user/group/channel namespaces)
- **Memory decay** with archive mechanism
- **SHA-256 content deduplication**
- **Optional sqlite-vec** hybrid retrieval (graceful degradation to pure FTS5)
- **Chinese tokenization** via jieba (optional, degrades to char-level if missing)
- **Three-tier persona evolution** (Tier 3 default off)
- **Dual-recall routing** for group chats (sender + group context)

#### Changed

- **API overhaul**:
  - Old: `memory_update`, `memory_query`, `memory_clear`, `consolidate_memory`
  - New: `memory_add`, `memory_search`, `memory_update_entry`, `memory_remove`, `profile_view`, `profile_update`
- **Storage layout** moved from `kiraos.db` to `data/memory/` tree
- **Profile format** changed from `profile.md` (YAML frontmatter) to `profile.json`

#### Migration

- Automatic v2 → v3 migration on first launch (`auto_migrate_legacy_db: true`)
- `user_profiles` rows → entity profiles (category-based field mapping)
- `event_logs` rows → fact TOML files
- Old database backed up as `kiraos.db.bak_<timestamp>`

---

## [2.x] - Legacy

v2.x used simple SQLite tables (`user_profiles`, `event_logs`) with limited per-user memory. See git history for details.

[4.0.0]: https://github.com/LyaQanYi/KiraOS_Plugin/compare/v3.0.1...v4.0.0
[3.0.1]: https://github.com/LyaQanYi/KiraOS_Plugin/compare/v3.0.0...v3.0.1
[3.0.0]: https://github.com/LyaQanYi/KiraOS_Plugin/compare/v2.x...v3.0.0
