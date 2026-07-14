"""Shared CLI presentation: result rendering, candidate lists, confirm flow."""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlmodel import Session

from jobtrack.cli.context import AppState
from jobtrack.core import proposals as props
from jobtrack.core.proposals import CommitError, ProposalError
from jobtrack.llm.tools import ToolContext, ToolResult, dispatch

console = Console()


def print_candidates(result: ToolResult) -> None:
    console.print("[yellow]Ambiguous — candidates:[/yellow]")
    for i, cand in enumerate(result.candidates or [], start=1):
        activity = f"last activity {cand.last_activity}" if cand.last_activity else None
        extra = " · ".join(p for p in [cand.status, activity] if p)
        suffix = f"  ({extra})" if extra else ""
        console.print(f"  {i}. {cand.label}  [{cand.ref}]{suffix}")


def print_hits(data: dict[str, Any]) -> None:
    hits = data.get("hits", [])
    if not hits:
        console.print("[dim]no results[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("Ref")
    table.add_column("Type")
    table.add_column("Match")
    table.add_column("Score", justify="right")
    for hit in hits:
        headline = hit["headline"]
        if hit.get("snippet"):
            headline += f"  [dim]{hit['snippet'][:60]}[/dim]"
        table.add_row(hit["ref"], hit["type"], headline, f"{hit['score']:.0f}")
    console.print(table)


def print_ok(result: ToolResult) -> None:
    if result.data and "hits" in result.data:
        print_hits(result.data)
    elif result.data is not None:
        console.print_json(json.dumps(result.data, default=str))
    if result.message:
        console.print(f"[dim]{result.message}[/dim]")


def confirm_flow(
    session: Session, result: ToolResult, *, yes: bool = False
) -> bool:
    """Show the proposal diff, ask, commit or reject. Returns True if committed."""
    proposal = result.proposal
    assert proposal is not None
    console.print(
        Panel(proposal.diff, title=f"proposal #{proposal.id} — {proposal.kind}", expand=False)
    )
    if result.message:
        console.print(f"[dim]{result.message}[/dim]")

    if yes and proposal.auto_approvable:
        approved = True
    else:
        approved = typer.confirm("Commit?", default=True)

    if not approved:
        props.reject(session, proposal.id, "declined at prompt")
        session.commit()
        console.print("[yellow]rejected[/yellow]")
        return False
    try:
        commit_result = props.confirm(session, proposal.id)
    except (ProposalError, CommitError) as exc:
        session.rollback()
        console.print(f"[red]commit failed: {exc}[/red]")
        return False
    session.commit()
    console.print(f"[green]{commit_result.summary}[/green]")
    return True


def dispatch_interactive(
    state: AppState,
    session: Session,
    name: str,
    args: dict[str, Any],
    *,
    yes: bool = False,
    source: str = "cli",
    utterance: str | None = None,
) -> None:
    """dispatch() + interactive disambiguation + confirm flow."""
    ctx = ToolContext(
        session=session,
        engine=state.engine,
        config=state.config,
        source=source,
        utterance=utterance,
    )
    args = {k: v for k, v in args.items() if v is not None}
    result = dispatch(ctx, name, args)
    for _ in range(3):
        if result.result != "needs_disambiguation":
            break
        print_candidates(result)
        answer = typer.prompt("Pick a number (or 'q' to cancel)")
        if answer.strip().casefold() == "q":
            return
        try:
            index = int(answer) - 1
            chosen = (result.candidates or [])[index]
        except (ValueError, IndexError):
            console.print("[red]invalid choice[/red]")
            return
        param = (result.data or {}).get("disambiguate_param", "ref")
        args[str(param)] = chosen.ref
        result = dispatch(ctx, name, args)

    if result.result == "error":
        console.print(f"[red]{result.message}[/red]")
        raise typer.Exit(1)
    if result.result == "needs_disambiguation":
        print_candidates(result)
        console.print("[yellow]still ambiguous — use an explicit ref like app#12[/yellow]")
        raise typer.Exit(1)
    if result.result == "ok":
        session.commit()
        print_ok(result)
        return
    confirm_flow(session, result, yes=yes)
