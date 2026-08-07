"""skill — load a listed skill's full instructions on demand.

Mirrors Hermes' ``skill_view`` tool. The Skills section of the system prompt
lists only skill names and one-line descriptions to keep context small; this
tool lets the model pull in the full ``SKILL.md`` body for a specific skill the
moment a request matches it — instead of either holding every skill's
instructions in context permanently (``always: true``) or relying on the model
to guess a ``read_file`` path. This is what makes progressive (``always:
false``) skills actually usable by any model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema(
            "Skill name exactly as shown in the Skills list (e.g. 'weather')."
        ),
        required=["name"],
    )
)
class SkillTool(Tool):
    """Load a listed skill's full instructions by name."""

    _scopes = {"core", "subagent"}

    name = "skill"  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
    description = (  # pyright: ignore[reportIncompatibleMethodOverride, reportAssignmentType]
        "Load the full instructions of an available skill by name (from the "
        "Skills list). Call this the moment a request matches a listed skill, "
        "then follow the returned steps and commands — before reaching for "
        "generic tools like web_search/web_fetch or improvising."
    )

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(workspace=Path(ctx.workspace))

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def execute(self, name: str = "", **_: Any) -> Any:
        skill_name = (name or "").strip()
        loader = SkillsLoader(self._workspace)
        available = [entry["name"] for entry in loader.list_skills(filter_unavailable=False)]
        if not skill_name:
            return "Provide a skill name. Available skills: " + ", ".join(available)
        if skill_name not in available:
            return (
                f"Skill '{skill_name}' not found. Available skills: "
                + ", ".join(available)
            )
        can_run, reason = loader.get_skill_availability(skill_name)
        if not can_run:
            return f"Skill '{skill_name}' is currently unavailable: {reason}"
        markdown = loader.load_skill(skill_name)
        if not markdown:
            return f"Skill '{skill_name}' could not be loaded."
        body = loader._strip_frontmatter(markdown)
        return f"# Skill: {skill_name}\n\n{body}"
