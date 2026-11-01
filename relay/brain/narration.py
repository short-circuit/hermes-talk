"""Narration reads mapped phrases, never raw tool output.

Tool output is untrusted data: it is sanitized and truncated with an explicit
marker before it is spoken.  Narration can never approve anything; approval is
the spoken gate's job.  No network calls are made here.
"""

from __future__ import annotations

from collections.abc import Mapping

from relay.brain.delegation import truncate_untrusted

MAX_NARRATION_CHARS = 160
TOOL_PHRASES: Mapping[str, str] = {
    "read_file": "Reading the file.",
    "write_file": "Writing the file.",
    "edit_file": "Editing the file.",
    "terminal": "Running a command.",
    "web_search": "Searching the web.",
    "web_fetch": "Fetching the page.",
    "delegate_task": "Delegating the task.",
    "kanban_complete": "Completing the card.",
    "approval": "Waiting for approval.",
    "default": "Working on it.",
}
DEFAULT_TOOL_PHRASE = "Working on it."


def phrase_for_tool(tool: str) -> str:
    """Return the fixed phrase for a tool; never echo the raw tool name."""
    return TOOL_PHRASES.get(tool, DEFAULT_TOOL_PHRASE)


def narrate_tool_started(tool: str) -> str:
    """Return the mapped phrase for a tool that just started."""
    return phrase_for_tool(tool)


def narrate_tool_completed(tool: str, output: str) -> str:
    """Return the mapped phrase plus a bounded, visibly marked excerpt."""
    phrase = phrase_for_tool(tool)
    excerpt = truncate_untrusted(output, MAX_NARRATION_CHARS).text
    if not excerpt:
        return phrase
    return f"{phrase} {excerpt}"


def narrate_run_completed() -> str:
    """Return the fixed completion phrase."""
    return "Done."


__all__ = [
    "DEFAULT_TOOL_PHRASE",
    "MAX_NARRATION_CHARS",
    "TOOL_PHRASES",
    "narrate_run_completed",
    "narrate_tool_completed",
    "narrate_tool_started",
    "phrase_for_tool",
]
