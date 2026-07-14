"""`brief`, `stats`, `skills`, `questions` — the insight engine's CLI face."""

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.table import Table

from jobtrack.cli.context import get_state
from jobtrack.cli.render import console
from jobtrack.core import analytics
from jobtrack.core.clock import to_local
from jobtrack.core.db import session_scope
from jobtrack.core.prep import question_bank


def brief() -> None:
    """One screen: interviews in 48h, tasks due, stale apps, pending proposals."""
    state = get_state()
    with session_scope(state.engine) as session:
        data = analytics.briefing(session, state.config.sla_days)

        if data.interviews_next_48h:
            lines = [
                f"{to_local(iv.scheduled_at).strftime('%a %H:%M')} — {iv.label} "
                f"(round {iv.round}, {iv.format})"
                for iv in data.interviews_next_48h
            ]
            console.print(Panel("\n".join(lines), title="Interviews next 48h", style="bold"))
        else:
            console.print("[dim]no interviews in the next 48h[/dim]")

        if data.tasks_due:
            lines = [
                ("[red]OVERDUE[/red] " if task.overdue else "")
                + f"{to_local(task.due_at).strftime('%a %H:%M')} — {task.description}"
                + (f" ({task.app_label})" if task.app_label else "")
                for task in data.tasks_due
            ]
            console.print(Panel("\n".join(lines), title="Tasks due"))

        if data.stale:
            lines = [
                f"{item.label} — {item.status}, quiet {item.days_quiet}d"
                for item in data.stale
            ]
            console.print(Panel("\n".join(lines), title="Past SLA (stale/ghosted)"))

        if data.pending_proposals:
            lines = [
                f"#{prop.ref.split('#')[1]} {prop.kind} ({prop.source}) {prop.summary}"
                for prop in data.pending_proposals
            ]
            console.print(
                Panel(
                    "\n".join(lines) + "\n[dim]run `jobtrack review`[/dim]",
                    title=f"{len(data.pending_proposals)} pending proposal(s)",
                )
            )


def _rate_table(title: str, rows: list[analytics.GroupStat]) -> None:
    table = Table(title=title, show_header=True, header_style="bold")
    for column in ("Group", "Apps", "Responses", "Resp %", "Positive", "Pos %"):
        table.add_column(column, justify="right")
    table.columns[0].justify = "left"
    for row in rows:
        table.add_row(
            row.label,
            str(row.applications),
            str(row.responses),
            f"{row.response_rate:.0%}",
            str(row.positive),
            f"{row.positive_rate:.0%}",
        )
    console.print(table)


def stats(
    which: str = typer.Argument("all", help="funnel|resume|source|timing|all"),
) -> None:
    """Funnel conversion and response rates by resume version, source, timing."""
    state = get_state()
    with session_scope(state.engine) as session:
        if which in ("funnel", "all"):
            table = Table(title="Funnel", show_header=True, header_style="bold")
            for column in ("Stage", "Reached", "Progressed", "Conversion", "Median days"):
                table.add_column(column, justify="right")
            table.columns[0].justify = "left"
            for stage in analytics.funnel(session):
                if stage.reached == 0:
                    continue
                table.add_row(
                    stage.stage,
                    str(stage.reached),
                    str(stage.progressed),
                    f"{stage.conversion:.0%}" if stage.conversion is not None else "-",
                    str(stage.median_days_to_next)
                    if stage.median_days_to_next is not None
                    else "-",
                )
            console.print(table)
        if which in ("resume", "all"):
            _rate_table("Response rate by resume version", analytics.by_resume(session))
        if which in ("source", "all"):
            _rate_table("Response rate by application source", analytics.by_source(session))
        if which in ("timing", "all"):
            rows = analytics.by_time_to_apply(session)
            if rows:
                _rate_table("Response rate by time-to-apply (posted -> applied)", rows)
            else:
                console.print(
                    "[dim]timing: no applications with both posted_at and applied_at yet[/dim]"
                )


def skills(
    extract: bool = typer.Option(False, "--extract", help="Extract skills from JDs first (LLM)"),
) -> None:
    """Skill gap map: what targeted JDs ask for vs what the active resume evidences."""
    state = get_state()
    with session_scope(state.engine) as session:
        if extract:
            from jobtrack.llm.provider import ProviderError
            from jobtrack.llm.router import provider_for
            from jobtrack.llm.skills import extract_missing

            try:
                jobs_done, added = extract_missing(session, provider_for(state.config, "jd_parse"))
                console.print(f"[dim]extracted {added} skills from {jobs_done} JD(s)[/dim]")
            except ProviderError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from exc
        gap, resume_label, jobs_covered = analytics.skill_gap(session)
        if not gap:
            console.print(
                "[dim]no skills extracted yet — run `jobtrack skills --extract` "
                "after capturing JDs[/dim]"
            )
            return
        table = Table(
            title=f"Skills across {jobs_covered} targeted JD(s) vs resume "
            f"{resume_label or '(none registered)'}",
            show_header=True,
            header_style="bold",
        )
        for column in ("Skill", "JDs", "% of JDs", "Evidenced in resume?"):
            table.add_column(column)
        for row in gap[:40]:
            table.add_row(
                row.skill,
                str(row.jobs),
                f"{row.pct:.0%}",
                "[green]yes[/green]" if row.in_resume else "[red]no[/red]",
            )
        console.print(table)
        missing = [row for row in gap if not row.in_resume and row.pct >= 0.3]
        if missing and resume_label:
            top = missing[0]
            console.print(
                f"\n[bold]{top.skill}[/bold] appears in {top.pct:.0%} of the roles you're "
                f"targeting and is not evidenced in {resume_label}."
            )


def questions(
    company: str | None = typer.Option(None, help="Filter by company name"),
) -> None:
    """Your personal interview question bank, built from debriefs."""
    state = get_state()
    with session_scope(state.engine) as session:
        rows = question_bank(session)
        if company:
            needle = company.casefold()
            rows = [row for row in rows if needle in row[0].casefold()]
        if not rows:
            console.print("[dim]no questions recorded yet — they accumulate from debriefs[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        for column in ("Company", "Round", "Question"):
            table.add_column(column)
        for name, round_no, question in rows:
            table.add_row(name, str(round_no), question)
        console.print(table)
