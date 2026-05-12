"""
Slim MemoryManager — fast/slow loop facade for KiraOS_Plugin.

Adapted from KiraAI-lightning's MemoryManager. Lightning's version owns
the bot's full IM message-loop (session_memory, MemoryRouter, chat history
buffering); KiraAI already handles those upstream via
``KiraMessageBatchEvent``, so this slim variant exposes only:

  - ``recall(query, entity_id, k)``               — fast-loop retrieval
  - ``format_recalled_memories(memories)``        — prompt formatting helper
  - ``get_profile / get_profile_prompt``          — entity profile access
  - ``update_user_interaction``                   — bump interaction counts
  - ``process_turn(user_text, assistant_reply, entity_id, ...)``
        single-entity hippocampus entry point (extract → dedupe → store →
        elevate → profile sync)
  - ``run_forgetting_cycle``                      — periodic GC trigger
  - ``close``                                     — release DB handles
"""

import asyncio
from typing import List

from core.logging_manager import get_logger

from .memory_index import MemoryIndex
from .toml_tree_store import TomlTreeStore, Memory
from .entity_profile import EntityProfileStore, EntityProfile
from .memory_extractor import MemoryExtractor
from .memory_decay import MemoryDecayEngine
from .memory_paths import (
    ensure_directory_structure,
    ENTITY_USER,
    ENTITY_GROUP,
)

logger = get_logger("kiraos_memory_manager", "green")


class MemoryManager:
    """Slim facade combining the storage, profile, extractor and decay layers."""

    def __init__(
        self,
        index: MemoryIndex = None,
        tree_store: TomlTreeStore = None,
        profile_store: EntityProfileStore = None,
        extractor: MemoryExtractor = None,
        decay_engine: MemoryDecayEngine = None,
        llm_client=None,
        fast_llm_client=None,
    ):
        ensure_directory_structure()

        self.index = index or MemoryIndex()
        self.tree_store = tree_store or TomlTreeStore(index=self.index)
        self.profile_store = profile_store or EntityProfileStore()
        self.extractor = extractor or MemoryExtractor(self.tree_store, llm_client)
        if fast_llm_client is not None:
            self.extractor.set_fast_llm_client(fast_llm_client)
        elif llm_client is not None:
            # Lightning's pattern: same client serves both buckets if the
            # provider routes via chat_fast() internally.
            self.extractor.set_fast_llm_client(llm_client)
        self.decay_engine = decay_engine or MemoryDecayEngine(self.tree_store)

        self._llm_client = llm_client
        logger.info("MemoryManager initialized (slim variant)")

    def set_llm_client(self, llm_client):
        self._llm_client = llm_client
        self.extractor.set_llm_client(llm_client)

    def set_fast_llm_client(self, fast_llm_client):
        self.extractor.set_fast_llm_client(fast_llm_client)

    async def async_init(self):
        """Optional startup hook: rebuild SQLite index from TOML files.

        Useful when users hand-edit the on-disk TOML between runs. Safe to
        skip (the index is also kept in sync on every write).
        """
        try:
            await self.tree_store.rebuild_index()
        except Exception as e:
            logger.warning(f"Index rebuild failed during async_init: {e}")

    # ==========================================
    # Fast loop — recall
    # ==========================================

    async def recall(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "user",
        k: int = 5,
    ) -> List[Memory]:
        """Retrieve relevant long-term memories for the current turn.

        Searches facts + reflections jointly. Does *not* bump access counters
        — that happens only when the LLM actually cites the memory.
        """
        try:
            k = max(1, int(k))
        except (TypeError, ValueError):
            k = 5

        if not entity_id:
            return []

        try:
            return await self.tree_store.search_across_folders(
                query=query,
                entity_id=entity_id,
                entity_type=entity_type,
                folders=["facts", "reflections"],
                k=k,
            )
        except Exception as e:
            logger.error(f"Recall error: {e}")
            return []

    @staticmethod
    def format_recalled_memories(memories: List[Memory]) -> str:
        if not memories:
            return ""

        type_labels = {
            "fact": "事实",
            "reflection": "洞察",
            "episodic": "事件",
            "skill": "技能",
            "summary": "摘要",
        }
        parts = []
        for mem in memories:
            label = type_labels.get(mem.type, mem.type)
            tags_str = f" [{', '.join(mem.tags)}]" if mem.tags else ""
            parts.append(f"[{label}]{tags_str} {mem.raw_text}")
        return "\n".join(parts)

    # ==========================================
    # Entity profile
    # ==========================================

    async def get_profile(
        self, entity_id: str, entity_type: str = ENTITY_USER
    ) -> EntityProfile:
        return await self.profile_store.get_profile(entity_id, entity_type)

    async def get_profile_prompt(
        self, entity_id: str, entity_type: str = ENTITY_USER
    ) -> str:
        return await self.profile_store.get_profile_prompt(entity_id, entity_type)

    async def update_user_interaction(
        self, user_id: str, platform: str = "", nickname: str = ""
    ):
        """Bump interaction count + sync display fields.

        When ``nickname`` changes, the previous one is archived into the
        profile's ``aliases`` list automatically by EntityProfileStore.
        """
        updates = {}
        if platform:
            updates["platform"] = platform
        if nickname:
            updates["nickname"] = nickname
            profile = await self.profile_store.get_profile(user_id, ENTITY_USER)
            if not profile.name:
                updates["name"] = nickname
        await self.profile_store.increment_interaction(
            user_id, ENTITY_USER, **updates
        )

    # ==========================================
    # Slow loop — hippocampus
    # ==========================================

    async def process_turn(
        self,
        conversation_text: str,
        entity_id: str,
        entity_type: str = ENTITY_USER,
        *,
        is_group: bool = False,
        group_entity_id: str = "",
    ) -> dict:
        """Single-entity hippocampus pass for the just-finished turn.

        ``conversation_text`` should already include speaker labels in the
        ``昵称(ID): 内容`` shape that the extractor prompts expect; the
        caller is responsible for assembling it from KiraAI's message
        batch.

        For private chats: extract personal facts, route them all to the
        single user entity.
        For group chats: run both personal + group extraction in parallel,
        store group-level facts under ``group_entity_id``.

        Returns a small summary dict for logging.
        """
        if not self._llm_client:
            logger.debug("LLM client not set, skipping hippocampus")
            return {"personal": 0, "group": 0}

        if not entity_id:
            return {"personal": 0, "group": 0}

        try:
            if is_group and group_entity_id:
                personal_facts, group_facts = await asyncio.gather(
                    self.extractor.extract_personal_facts(conversation_text),
                    self.extractor.extract_group_facts(conversation_text),
                )
            else:
                personal_facts = await self.extractor.extract_facts(conversation_text)
                group_facts = []

            if not personal_facts and not group_facts:
                return {"personal": 0, "group": 0}

            routed_entities = set()

            for fact in personal_facts:
                # Slim version: all personal facts route to the speaker
                # passed in by the caller. Lightning's full router resolves
                # per-fact speakers from sender_map; that path requires
                # access to chunk metadata that KiraOS_Plugin doesn't pass
                # in here.
                await self.extractor.deduplicate_and_store(
                    fact, entity_id, entity_type
                )
                routed_entities.add((entity_id, entity_type))

                # Promote high-importance facts into the profile (charter §4.3).
                content = fact.get("content", "")
                importance = fact.get("importance", 5)
                if importance >= 7 and content and entity_type == ENTITY_USER:
                    try:
                        await self.profile_store.add_fact(
                            entity_id, content, entity_type
                        )
                    except Exception as e:
                        logger.warning(f"Profile fact sync failed: {e}")

            for fact in group_facts:
                await self.extractor.deduplicate_and_store(
                    fact, group_entity_id, ENTITY_GROUP
                )
                routed_entities.add((group_entity_id, ENTITY_GROUP))

            # Elevation check on every entity that was touched.
            for eid, etype in routed_entities:
                try:
                    if await self.extractor.check_elevation_trigger(eid, etype):
                        await self.extractor.generate_reflections(eid, etype)
                except Exception as e:
                    logger.warning(f"Elevation failed for {etype}:{eid}: {e}")

            summary = {
                "personal": len(personal_facts),
                "group": len(group_facts),
            }
            logger.info(
                f"Hippocampus completed for {entity_type}:{entity_id}: "
                f"{summary['personal']} personal + {summary['group']} group facts"
            )
            return summary
        except Exception as e:
            logger.error(f"Hippocampus error: {e}", exc_info=True)
            return {"personal": 0, "group": 0}

    # ==========================================
    # Decay
    # ==========================================

    async def run_forgetting_cycle(self) -> tuple[int, int]:
        try:
            removed, downgraded = await self.decay_engine.run_full_cycle()
            if removed or downgraded:
                logger.info(
                    f"Forgetting cycle: removed={removed}, downgraded={downgraded}"
                )
            return removed, downgraded
        except Exception as e:
            logger.error(f"Forgetting cycle error: {e}")
            return 0, 0

    # ==========================================
    # Lifecycle
    # ==========================================

    def close(self):
        try:
            self.index.close()
        except Exception as e:
            logger.warning(f"Index close error: {e}")
