"""
Skill Router — Progressive Disclosure for KiraAI.

Implements the same pattern as Claude's Skill system:
  - At startup: scan skill folders, load only lightweight metadata as Tool definitions.
  - At runtime: when LLM triggers a skill, lazy-load the full instruction body and
    return it as the tool result — the main LLM reads and executes the instruction
    in the SAME tool-loop turn, with ZERO extra API calls.
  - On demand: the LLM may also pull additional resource files bundled with the
    skill via the `read_skill_resource` tool (third disclosure tier).

Two on-disk formats are accepted per skill folder:

  Format A (preferred, single file):
      <skill>/SKILL.md
        ---
        name: tarot_reading
        description: 当用户明确要求进行塔罗牌占卜...
        exclude: 用户只是随口提到占卜...
        command: /tarot
        parameters:
          type: object
          properties:
            question: {type: string, description: 用户想要占卜的问题}
          required: [question]
        ---
        # 塔罗牌占卜技能
        ...
      Placeholders use Jinja-style `{{question}}`.

  Format B (legacy, two files):
      <skill>/manifest.json
      <skill>/instruction.md
      Placeholders use single-brace `{question}` (kept for backward compat).
"""

import json
import re
from pathlib import Path
from typing import Dict, Optional, List, Set, Tuple

from core.logging_manager import get_logger

logger = get_logger("skill_router", "purple")

# Re-exported so main.py can advertise the resource-tier tool only when relevant.
RESOURCE_DIR_NAMES = ("references", "resources", "scripts", "data")


def _parse_skill_md(text: str) -> Tuple[Optional[dict], str]:
    """Split a SKILL.md into (frontmatter_dict, body_text).

    Frontmatter is YAML between two `---` fences at the top of the file.
    Returns (None, full_text) if no frontmatter is present or parsing fails.
    """
    if not text.lstrip().startswith("---"):
        return None, text
    # Locate the opening and closing fences (allow leading whitespace/BOM)
    stripped = text.lstrip()
    leading = len(text) - len(stripped)
    rest = stripped[3:]
    # Closing fence must be on its own line
    m = re.search(r"^---[ \t]*$", rest, re.MULTILINE)
    if not m:
        return None, text
    fm_text = rest[: m.start()]
    body = rest[m.end():]
    # Strip a single leading newline from body for cleanliness
    if body.startswith("\n"):
        body = body[1:]
    elif body.startswith("\r\n"):
        body = body[2:]
    try:
        import yaml  # PyYAML is a project dependency
        fm = yaml.safe_load(fm_text)
    except Exception as e:
        logger.warning(f"Failed to parse SKILL.md frontmatter: {e}")
        return None, text
    if not isinstance(fm, dict):
        return None, text
    return fm, body


class SkillInfo:
    """Parsed metadata for a single skill."""

    __slots__ = (
        "skill_id", "name", "description", "trigger", "exclude", "command",
        "parameters", "instruction_path", "manifest_path", "root_path", "format",
        "_instruction_body", "_instruction_cache", "_declared_params",
    )

    def __init__(self, skill_id: str, name: str, description: str,
                 parameters: dict, instruction_path: Optional[Path],
                 manifest_path: Optional[Path], root_path: Path, *,
                 trigger: str = "", exclude: str = "", command: str = "",
                 fmt: str = "legacy", instruction_body: Optional[str] = None):
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
        self.format = fmt  # "skill_md" or "legacy"
        # For SKILL.md the body is already in memory; cache it eagerly.
        self._instruction_body = instruction_body
        self._instruction_cache: Optional[str] = instruction_body
        # Extract declared parameter names from JSON Schema for safe substitution
        props = parameters.get("properties", {})
        self._declared_params: Set[str] = set(props.keys()) if isinstance(props, dict) else set()

    def load_instruction(self) -> str:
        """Read the instruction body — cached after first load."""
        if self._instruction_cache is not None:
            return self._instruction_cache
        if self.instruction_path and self.instruction_path.exists():
            self._instruction_cache = self.instruction_path.read_text(encoding="utf-8")
            return self._instruction_cache
        return ""

    def clear_cache(self):
        """Clear the cached instruction so the next load re-reads from disk.

        For SKILL.md skills the body is read once during discover(); a true
        refresh requires the SkillRouter to re-discover.
        """
        self._instruction_cache = self._instruction_body  # may be None for legacy

    @property
    def tool_description(self) -> str:
        """Description shown to the LLM at tool registration.

        Prefers `trigger` only when present (legacy compatibility). New SKILL.md
        skills are encouraged to write a single trigger-aware `description`.
        """
        return self.trigger or self.description

    def has_resources(self) -> bool:
        """True if the skill folder has any of the conventional resource subdirs."""
        for name in RESOURCE_DIR_NAMES:
            if (self.root_path / name).is_dir():
                return True
        return False

    def __repr__(self):
        return f"<Skill {self.name!r} @ {self.root_path}>"


class SkillRouter:
    """
    Scans a directory for skill folders, parses metadata,
    and provides a factory for building instruction prompts.
    """

    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir)
        self.skills: Dict[str, SkillInfo] = {}

    # ── Discovery ──────────────────────────────────────────────────

    def discover(self) -> list[SkillInfo]:
        """Scan skills_dir for skill folders. Returns newly discovered SkillInfo objects."""
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

            skill = self._parse_skill_dir(entry)
            if skill is None:
                continue
            if skill.name in self.skills:
                existing = self.skills[skill.name]
                logger.warning(
                    f"Duplicate skill name '{skill.name}': {entry.name} "
                    f"conflicts with {existing.root_path.name}, skipping"
                )
                continue
            self.skills[skill.name] = skill
            discovered.append(skill)
            tag = "SKILL.md" if skill.format == "skill_md" else "manifest.json"
            cmd = f" [cmd: {skill.command}]" if skill.command else ""
            res = " [+resources]" if skill.has_resources() else ""
            logger.info(f"Discovered skill: {skill.name} ({entry.name}, {tag}){cmd}{res}")

        return discovered

    def _parse_skill_dir(self, entry: Path) -> Optional[SkillInfo]:
        """Try SKILL.md first, then fall back to manifest.json + instruction.md.

        Previously a present-but-malformed ``SKILL.md`` would short-circuit and
        the skill was silently dropped, even when a working legacy
        ``manifest.json + instruction.md`` pair sat next to it. We now treat
        an unparseable ``SKILL.md`` as "not parsed" and continue to the legacy
        check (matching what the docstring already promised).
        """
        skill_md = entry / "SKILL.md"
        if skill_md.is_file():
            skill = self._parse_skill_md_file(entry, skill_md)
            if skill is not None:
                return skill
            # SKILL.md exists but was unparseable. Warn loudly so the operator
            # notices, then fall through to the legacy attempt.
            logger.warning(
                f"Skill {entry.name}: SKILL.md present but failed to parse, "
                "falling back to manifest.json + instruction.md if available"
            )

        manifest_path = entry / "manifest.json"
        if manifest_path.is_file():
            return self._parse_legacy(entry, manifest_path)

        return None

    def _parse_skill_md_file(self, entry: Path, skill_md: Path) -> Optional[SkillInfo]:
        try:
            text = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to read {skill_md}: {e}")
            return None
        fm, body = _parse_skill_md(text)
        if fm is None:
            logger.warning(f"Skill {entry.name}/SKILL.md missing or invalid YAML frontmatter, skipping")
            return None
        meta = self._normalize_metadata(entry, fm)
        if meta is None:
            return None
        if not body.strip():
            logger.warning(f"Skill {entry.name}/SKILL.md has empty body, skipping")
            return None
        return SkillInfo(
            skill_id=entry.name,
            name=meta["name"],
            description=meta["description"],
            parameters=meta["parameters"],
            instruction_path=skill_md,
            manifest_path=skill_md,
            root_path=entry,
            trigger=meta["trigger"],
            exclude=meta["exclude"],
            command=meta["command"],
            fmt="skill_md",
            instruction_body=body,
        )

    def _parse_legacy(self, entry: Path, manifest_path: Path) -> Optional[SkillInfo]:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to parse manifest in {entry.name}: {e}")
            return None
        if not isinstance(manifest, dict):
            logger.warning(f"Manifest for {entry.name} is not a JSON object, skipping")
            return None

        instruction_path = entry / "instruction.md"
        if not instruction_path.exists():
            logger.warning(f"Skill {entry.name} has manifest but no instruction.md, skipping")
            return None

        meta = self._normalize_metadata(entry, manifest)
        if meta is None:
            return None
        return SkillInfo(
            skill_id=entry.name,
            name=meta["name"],
            description=meta["description"],
            parameters=meta["parameters"],
            instruction_path=instruction_path,
            manifest_path=manifest_path,
            root_path=entry,
            trigger=meta["trigger"],
            exclude=meta["exclude"],
            command=meta["command"],
            fmt="legacy",
        )

    @staticmethod
    def _normalize_metadata(entry: Path, raw: dict) -> Optional[dict]:
        """Validate and coerce metadata fields. Returns None if mandatory fields invalid."""
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            logger.warning(f"Skill {entry.name} has invalid or missing 'name', skipping")
            return None
        name = name.strip()

        def _str(field: str) -> str:
            v = raw.get(field, "")
            return v.strip() if isinstance(v, str) else ""

        command = _str("command")
        if "command" in raw and not isinstance(raw.get("command"), str):
            logger.warning(f"Skill {entry.name} has non-string 'command', ignoring")
            command = ""

        parameters = raw.get("parameters", {"type": "object", "properties": {}, "required": []})
        if not isinstance(parameters, dict):
            logger.warning(f"Skill {entry.name} has invalid 'parameters', using default")
            parameters = {"type": "object", "properties": {}, "required": []}

        return {
            "name": name,
            "description": _str("description"),
            "trigger": _str("trigger"),
            "exclude": _str("exclude"),
            "command": command,
            "parameters": parameters,
        }

    # ── Routing helpers ────────────────────────────────────────────

    def reload(self) -> list[SkillInfo]:
        """Clear all caches and re-discover skills."""
        for skill in self.skills.values():
            skill.clear_cache()
        return self.discover()

    def get_skill(self, name: str) -> Optional[SkillInfo]:
        return self.skills.get(name)

    def get_commands(self, enabled_only: Optional[Set[str]] = None) -> Dict[str, SkillInfo]:
        """Mapping of command string → SkillInfo for skills with commands.

        If *enabled_only* is provided, only skills whose name is in the set
        participate in conflict resolution.
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

    # ── Instruction & resource assembly ────────────────────────────

    def build_instruction_prompt(self, skill: SkillInfo, args: dict) -> str:
        """
        Load the instruction body and substitute argument placeholders.

        - SKILL.md format uses `{{param}}` (Jinja-style).
        - legacy manifest.json + instruction.md uses `{param}`.

        Each substituted value is XML-escaped and wrapped in `<user_input>` tags
        for prompt-injection defense; placeholders for optional params not
        provided are deleted. The skill's `exclude` clause is prepended as a
        guard hint.
        """
        template = skill.load_instruction()
        if not template:
            return ""

        required = set(skill.parameters.get("required", []))

        if skill.format == "skill_md":
            template = self._substitute(template, skill._declared_params, required, args,
                                        opener="{{", closer="}}")
        else:
            template = self._substitute(template, skill._declared_params, required, args,
                                        opener="{", closer="}")

        if skill.exclude:
            guard = (
                f"⚠️ 注意：以下情况不应执行此技能：{skill.exclude}\n"
                "如果当前情况符合排除条件，请忽略此技能指令，正常回复用户。\n\n"
            )
            template = guard + template

        return template

    @staticmethod
    def _substitute(template: str, declared: Set[str], required: Set[str],
                    args: dict, *, opener: str, closer: str) -> str:
        """Replace placeholders for declared parameters only. Safe against injection."""
        from xml.sax.saxutils import escape as xml_escape
        for param_name in declared:
            placeholder = f"{opener}{param_name}{closer}"
            if param_name in args and args[param_name] is not None:
                escaped = xml_escape(str(args[param_name]), {'"': '&quot;', "'": '&apos;'})
                safe_value = f"<user_input>{escaped}</user_input>"
                template = template.replace(placeholder, safe_value)
            elif param_name not in required:
                template = template.replace(placeholder, "")
        return template

    # ── Resource-tier (third level of progressive disclosure) ──────

    def list_resources(self, skill: SkillInfo) -> List[str]:
        """List all resource files (relative paths) bundled with the skill."""
        out: List[str] = []
        root = skill.root_path
        for sub in RESOURCE_DIR_NAMES:
            d = root / sub
            if not d.is_dir():
                continue
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    out.append(str(f.relative_to(root)).replace("\\", "/"))
        return out

    def read_resource(self, skill: SkillInfo, rel_path: str,
                      max_bytes: int = 200_000) -> Tuple[bool, str]:
        """Read a resource file inside the skill folder.

        Returns (ok, content_or_error_message). Refuses paths that escape the
        skill root or exceed *max_bytes*.
        """
        root = skill.root_path.resolve()
        # Reject obvious traversal up front
        if not rel_path or ".." in Path(rel_path).parts or Path(rel_path).is_absolute():
            return False, f"Error: invalid path '{rel_path}'"
        target = (root / rel_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return False, f"Error: path '{rel_path}' escapes skill root"
        # Only allow reads from designated resource dirs
        first = target.relative_to(root).parts[:1]
        if not first or first[0] not in RESOURCE_DIR_NAMES:
            allowed = ", ".join(RESOURCE_DIR_NAMES)
            return False, f"Error: only files under {allowed}/ may be read"
        if not target.is_file():
            return False, f"Error: file not found: {rel_path}"
        try:
            size = target.stat().st_size
            if size > max_bytes:
                return False, f"Error: file too large ({size} > {max_bytes} bytes)"
            return True, target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return False, f"Error: file '{rel_path}' is not valid UTF-8 text"
        except Exception as e:
            return False, f"Error: cannot read '{rel_path}': {e}"
