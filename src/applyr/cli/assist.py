"""LLM-assisted commands: say, mcp, prep, debrief, draft, reindex."""

from __future__ import annotations

import typer

from applyr.cli.context import get_state
from applyr.cli.render import console, dispatch_interactive
from applyr.core.clock import utcnow
from applyr.core.db import session_scope, vec_available
from applyr.core.models import Interview
from applyr.core.prep import PrepError, build_dossier
from applyr.llm import resolution as res
from applyr.llm.provider import ChatMessage, ProviderError
from applyr.llm.router import local_provider, provider_for
from applyr.llm.tools import ToolContext, dispatch


def say(
    text: str = typer.Argument(help="e.g. \"I heard back from Stripe, phone screen Friday\""),
    yes: bool = typer.Option(False, "--yes", "-y", help="Auto-commit safe actions"),
) -> None:
    """Natural-language capture through the local LLM (always confirms writes)."""
    state = get_state()
    from applyr.llm.say import plan

    with session_scope(state.engine) as session:
        ctx = ToolContext(
            session=session, engine=state.engine, config=state.config, source="say", utterance=text
        )
        try:
            response = plan(ctx, text)
        except ProviderError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        if not response.tool_calls:
            console.print(response.text or "[dim](the model proposed no action)[/dim]")
            return
        call = response.tool_calls[0]
        console.print(f"[dim]tool: {call.name}[/dim]")
        dispatch_interactive(
            state,
            session,
            call.name,
            dict(call.arguments),
            yes=yes,
            source="say",
            utterance=text,
        )


def mcp_serve() -> None:
    """Run the MCP server (stdio) for Claude Code / Claude Desktop."""
    from applyr.mcp.server import serve

    serve()


def prep(
    application: str,
    llm: bool = typer.Option(True, "--llm/--no-llm", help="Append LLM talking points"),
) -> None:
    """Generate a one-page interview prep dossier from everything on file."""
    state = get_state()
    with session_scope(state.engine) as session:
        resolved = res.resolve_application(session, application)
        if resolved.outcome != "resolved" or resolved.entity_id is None:
            console.print(f"[red]{resolved.hint or 'application not resolved'}[/red]")
            if resolved.candidates:
                for cand in resolved.candidates:
                    console.print(f"  - {cand.label} [{cand.ref}]")
            raise typer.Exit(1)
        try:
            dossier = build_dossier(session, resolved.entity_id)
        except PrepError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

        if llm:
            try:
                provider = provider_for(state.config, "prep")
                extra = provider.chat(
                    [
                        ChatMessage(
                            role="system",
                            content=(
                                "You are helping prepare for a job interview. Given the "
                                "dossier, write two short sections in markdown: "
                                "'## Likely questions' (5-8, grounded in the JD and previous "
                                "rounds) and '## Talking points' (3-5, matching the resume "
                                "to the JD's requirements). Be specific to this role; "
                                "no generic advice."
                            ),
                        ),
                        ChatMessage(role="user", content=dossier[:9000]),
                    ]
                )
                if extra.strip():
                    dossier += "\n\n" + extra.strip()
            except ProviderError as exc:
                console.print(f"[yellow]LLM section skipped: {exc}[/yellow]")

        filename = f"app{resolved.entity_id}-{utcnow().strftime('%Y%m%d-%H%M')}.md"
        path = state.config.prep_dir / filename
        path.write_text(dossier, encoding="utf-8")

        from sqlmodel import select

        upcoming = [
            iv
            for iv in session.exec(
                select(Interview).where(Interview.application_id == resolved.entity_id)
            ).all()
            if iv.scheduled_at >= utcnow()
        ]
        if upcoming:
            nearest = min(upcoming, key=lambda iv: iv.scheduled_at)
            nearest.prep_doc_path = str(path)
            session.add(nearest)

        console.print(f"[green]dossier written:[/green] {path}")
        preview = "\n".join(dossier.splitlines()[:25])
        console.print(f"[dim]{preview}\n...[/dim]")


def debrief(
    interview: str = typer.Argument(
        help="int#id or an application ref (picks the latest past round)"
    ),
) -> None:
    """Post-interview debrief: notes, questions asked, outcome."""
    state = get_state()
    with session_scope(state.engine) as session:
        resolved = res.resolve_interview(session, interview)
        if resolved.outcome != "resolved" or resolved.entity_id is None:
            console.print(f"[red]{resolved.hint or 'interview not resolved'}[/red]")
            raise typer.Exit(1)
        if resolved.note:
            console.print(f"[dim]{resolved.note}[/dim]")
        notes = typer.prompt("How did it go? (one line or a paragraph)")
        console.print("Questions they asked — one per line, empty line to finish:")
        questions: list[str] = []
        while True:
            line = typer.prompt("Q", default="", show_default=False)
            if not line.strip():
                break
            questions.append(line.strip())
        outcome = typer.prompt(
            "Outcome (advanced/rejected/pending/unknown)", default="pending"
        )
        dispatch_interactive(
            state,
            session,
            "log_debrief",
            {
                "interview": f"int#{resolved.entity_id}",
                "notes": notes,
                "questions_asked": questions,
                "outcome": outcome,
            },
        )


def draft(
    application: str,
    kind: str = typer.Option(
        "check_in", help="post_application_nudge|post_interview_thanks|check_in"
    ),
) -> None:
    """Draft a follow-up message (never sends anything)."""
    state = get_state()
    with session_scope(state.engine) as session:
        ctx = ToolContext(session=session, engine=state.engine, config=state.config, source="cli")
        result = dispatch(ctx, "draft_followup", {"application": application, "kind": kind})
        if result.result == "error":
            console.print(f"[red]{result.message}[/red]")
            raise typer.Exit(1)
        if result.result == "needs_disambiguation":
            from applyr.cli.render import print_candidates

            print_candidates(result)
            raise typer.Exit(1)
        console.print((result.data or {}).get("draft", ""))
        console.print(f"\n[dim]{result.message}[/dim]")


def reindex_cmd() -> None:
    """Rebuild the semantic search index (JDs, notes, debriefs, emails)."""
    state = get_state()
    if not vec_available(state.engine):
        console.print(
            "[red]sqlite-vec is not loadable in this Python — semantic search disabled. "
            "Fuzzy search still works.[/red]"
        )
        raise typer.Exit(1)
    from applyr.core.search import reindex

    with session_scope(state.engine) as session:
        provider = local_provider(state.config)
        try:
            count = reindex(state.engine, session, provider.embed, state.config.llm.embed_model)
        except ProviderError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(f"[green]indexed {count} item(s)[/green]")
