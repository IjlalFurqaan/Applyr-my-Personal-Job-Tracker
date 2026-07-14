"""`applyr say "..."` — natural language in, one tool call out.

The local model only picks a tool and fills arguments; resolution, validation,
diffing and confirmation all happen in deterministic code afterwards.
"""

from __future__ import annotations

from applyr.core.clock import utcnow
from applyr.llm.provider import ChatMessage, ChatResponse, ToolCallRequest
from applyr.llm.router import provider_for
from applyr.llm.tools import ToolContext, ToolResult, dispatch, tool_schemas


def _system_prompt() -> str:
    now = utcnow()
    return (
        "You are the capture layer of a personal job-application tracker. "
        "Convert the user's statement into exactly ONE tool call.\n"
        f"Today is {now.date().isoformat()} ({now.strftime('%A')}); current UTC time "
        f"{now.strftime('%H:%M')}. Convert relative dates ('yesterday', 'last Tuesday') "
        "to absolute ISO dates or datetimes.\n"
        "Pass company/application/contact references exactly as the user said them "
        "(e.g. 'Stripe', 'the Stripe backend role') — the tracker resolves them itself "
        "and will ask for disambiguation when needed.\n"
        "Never invent details the user did not state. If the statement is a question, "
        "use search, show or get_briefing. If nothing fits, reply in plain text instead "
        "of calling a tool."
    )


def plan(ctx: ToolContext, text: str) -> ChatResponse:
    provider = provider_for(ctx.config, "say")
    return provider.chat_with_tools(
        [
            ChatMessage(role="system", content=_system_prompt()),
            ChatMessage(role="user", content=text),
        ],
        tool_schemas(),
    )


def run_call(ctx: ToolContext, call: ToolCallRequest) -> ToolResult:
    return dispatch(ctx, call.name, dict(call.arguments))
