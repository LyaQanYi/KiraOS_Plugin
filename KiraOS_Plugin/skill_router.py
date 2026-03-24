"""
Skill Router — Progressive Disclosure for KiraAI.

Implements the same pattern as Claude's Skill system:
  - At startup: scan skill folders, load only lightweight manifest.json as Tool definitions
  - At runtime: when LLM triggers a skill, lazy-load instruction.md and return it
    as the tool result — the main LLM reads and executes the instruction in the
    SAME tool-loop turn, with ZERO extra API calls.

Each skill resides in its own directory under `data/skills/` and contains:
  - manifest.json   — compact tool definition (name, description, parameters)
  - instruction.md  — full execution rules, loaded only when triggered
"""

import json
import re
from pathlib import Path
from typing import Dict, Optional, List, Set

from core.logging_manager import get_logger

logger = get_logger("skill_router", "purple")


class SkillInfo:
    """Parsed metadata for a single skill."""

    __slots__ = (
        "skill_id", "name", "description", "trigger", "exclude", "command",
        "parameters", "instruction_path", "manifest_path", "root_path",
        "_instruction_cache", "_declared_params",
    )

    def __init__(self, skill_id: str, name: str, description: str,
                 parameters: dict, instruction_path: Path,
                 manifest_path: Path, root_path: Path, *,
                 trigger: str = "", exclude: str = "", command: str = ""):
        self.skill_id = skill_id
        self.name = name
        self.description = description
        self.trigger = trigger
        self.exclude = exclude
        self.command = command
        self.parameters = parameters
        self.instruction_path = instruction_path
        self.manifest_path = manifest_path
        self.root_path = root_path
        self._instruction_cache: str | None = None
        # Extract declared parameter names from JSON Schema for safe substitution
        props = parameters.get("properties", {})
        self._declared_params: Set[str] = set(props.keys()) if isinstance(props, dict) else set()

    def load_instruction(self) -> str:
        """Read instruction.md — cached after first load."""
        if self._instruction_cache is not None:
            return self._instruction_cache
        if self.instruction_path.exists():
            self._instruction_cache = self.instruction_path.read_text(encoding="utf-8")
            return self._instruction_cache
        return ""

    def clear_cache(self):
        """Clear the instruction cache so next load reads from disk."""
        self._instruction_cache = None

    @property
    def tool_description(self) -> str:
        """Return the best description for LLM tool registration.
        Prefers `trigger` (more precise) over `description`."""
        return self.trigger or self.description

    def __repr__(self):
        return f"<Skill {self.name!r} @ {self.root_path}>"
class SkillRouter:
    """
    Scans a directory for skill folders, parses manifests,
    and provides a factory for creating tool executor functions.
    """

    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir)
        self.skills: Dict[str, SkillInfo] = {}

    def discover(self) -> list[SkillInfo]:
        """
        Scan skills_dir for subdirectories containing manifest.json.
        Returns a list of newly discovered SkillInfo objects.
        """
        self.skills.clear()
        discovered = []

        if not self.skills_dir.exists():
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created skills directory: {self.skills_dir}")
            return discovered

        if not self.skills_dir.is_dir():
            logger.warning(f"Skills path exists but is not a directory: {self.skills_dir}")
            return discovered

        for entry in sorted(self.skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("_") or entry.name.startswith("."):
                continue

            manifest_path = entry / "manifest.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Failed to parse manifest in {entry.name}: {e}")
                continue

            if not isinstance(manifest, dict):
                logger.warning(f"Manifest for {entry.name} is not a JSON object, skipping")
                continue

            name = manifest.get("name")
            if not isinstance(name, str) or not name.strip():
                logger.warning(f"Skill {entry.name} has invalid or missing 'name', skipping")
                continue
            name = name.strip()

            description = manifest.get("description", "")
            description = description.strip() if isinstance(description, str) else ""
            trigger = manifest.get("trigger", "")
            trigger = trigger.strip() if isinstance(trigger, str) else ""
            exclude = manifest.get("exclude", "")
            exclude = exclude.strip() if isinstance(exclude, str) else ""
            command = manifest.get("command", "")
            if not isinstance(command, str):
                logger.warning(f"Skill {entry.name} has non-string 'command', ignoring")
                command = ""
            else:
                command = command.strip()
            parameters = manifest.get("parameters", {"type": "object", "properties": {}, "required": []})
            if not isinstance(parameters, dict):
                logger.warning(f"Skill {entry.name} has invalid 'parameters', using default")
                parameters = {"type": "object", "properties": {}, "required": []}

            instruction_path = entry / "instruction.md"
            if not instruction_path.exists():
                logger.warning(f"Skill {entry.name} has manifest but no instruction.md, skipping")
                continue

            skill = SkillInfo(
                skill_id=entry.name, name=name, description=description,
                parameters=parameters, instruction_path=instruction_path,
                manifest_path=manifest_path, root_path=entry,
                trigger=trigger, exclude=exclude, command=command,
            )
            if name in self.skills:
                existing = self.skills[name]
                logger.warning(
                    f"Duplicate skill name '{name}': {entry.name} "
                    f"conflicts with {existing.root_path.name}, skipping"
                )
                continue
            self.skills[name] = skill
            discovered.append(skill)
            logger.info(f"Discovered skill: {name} ({entry.name})"
                        + (f" [cmd: {command}]" if command else ""))

        return discovered

    def reload(self) -> list[SkillInfo]:
        """Clear all caches and re-discover skills."""
        for skill in self.skills.values():
            skill.clear_cache()
        return self.discover()

    def get_skill(self, name: str) -> Optional[SkillInfo]:
        return self.skills.get(name)

    def get_commands(self, enabled_only: Optional[Set[str]] = None) -> Dict[str, SkillInfo]:
        """Return a mapping of command string → SkillInfo for skills with commands.

        If *enabled_only* is provided, only skills whose name is in the set
        participate in conflict resolution, so a disabled skill cannot block
        an enabled one from claiming a command.
        """
        cmd_map: Dict[str, SkillInfo] = {}
        for s in self.skills.values():
            if not s.command:
                continue
            if enabled_only is not None and s.name not in enabled_only:
                continue
            if s.command in cmd_map:
                logger.warning(
                    f"Duplicate command '{s.command}': skill '{s.name}' "
                    f"conflicts with '{cmd_map[s.command].name}', keeping first"
                )
                continue
            cmd_map[s.command] = s
        return cmd_map
    def build_instruction_prompt(self, skill: SkillInfo, args: dict) -> str:
        """
        Load instruction.md and substitute argument placeholders.

        Improvements over simple str.replace:
          1. Only substitute parameters declared in manifest (prevents accidental replacement)
          2. Wrap substituted values in <user_input> tags (prompt injection defense)
          3. Clean up placeholders for optional params not provided
          4. Prepend exclude guard if skill has an exclude condition
        """
        template = skill.load_instruction()
        if not template:
            return ""

        # Only substitute declared parameters
        required = set(skill.parameters.get("required", []))
        for param_name in skill._declared_params:
            placeholder = f"{{{param_name}}}"
            if param_name in args and args[param_name] is not None:
                # XML-escape then wrap in tags for prompt injection defense
                from xml.sax.saxutils import escape as xml_escape
                escaped = xml_escape(str(args[param_name]), {'"': '&quot;', "'": '&apos;'})
                safe_value = f"<user_input>{escaped}</user_input>"
                template = template.replace(placeholder, safe_value)
            elif param_name not in required:
                # Optional param not provided — remove placeholder
                template = template.replace(placeholder, "")

        # Prepend exclude guard condition
        if skill.exclude:
            guard = f"⚠️ 注意：以下情况不应执行此技能：{skill.exclude}\n如果当前情况符合排除条件，请忽略此技能指令，正常回复用户。\n\n"
            template = guard + template

        return template
