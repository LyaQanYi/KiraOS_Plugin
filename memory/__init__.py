"""KiraOS_Plugin memory subsystem (ported from KiraAI-lightning).

Layout:
  - memory_paths    : directory structure + entity validation
  - memory_index    : SQLite + FTS5 + optional sqlite-vec
  - toml_tree_store : TOML file storage with index sync
  - entity_profile  : per-entity profile CRUD
  - memory_extractor: hippocampus (extract → dedupe → reflect)
  - memory_decay    : retention scoring + GC
  - memory_manager  : facade combining the above
"""
