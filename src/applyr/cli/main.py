"""Typer CLI. Every command is a thin wrapper over core / the tool registry —
the same propose→diff→confirm pipeline the LLM uses."""

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.table import Table

from applyr.cli import assist, emailcmd, insights
from applyr.cli.context import get_state
from applyr.cli.render import console, dispatch_interactive, print_ok
from applyr.core import proposals as props
from applyr.core.actions import AddCompany, NewCompany
from applyr.core.clock import to_local
from applyr.core.db import session_scope
from applyr.core.enums import DocumentType, Status
from applyr.core.events import days_in_stage, derived_status
from applyr.core.models import ApplicationEvent
from applyr.core.proposals import CommitError, ProposalError
from applyr.core.repos import applications as apps_repo
from applyr.core.repos import documents as documents_repo
from applyr.core.sla import days_since_activity, is_ghosted, is_stale
from applyr.llm import resolution as res
from applyr.llm.tools import ToolContext, dispatch

app = typer.Typer(no_args_is_help=True, help="Local-first job application tracker.")
add_app = typer.Typer(no_args_is_help=True, help="Add companies, jobs, contacts, docs, tasks.")
app.add_typer(add_app, name="add")
app.add_typer(emailcmd.email_app, name="email")

app.command("brief")(insights.brief)
app.command("stats")(insights.stats)
app.command("skills")(insights.skills)
app.command("questions")(insights.questions)
app.command("say")(assist.say)
app.command("mcp")(assist.mcp_serve)
app.command("ui")(assist.ui)
app.command("prep")(assist.prep)
app.command("debrief")(assist.debrief)
app.command("reindex")(assist.reindex_cmd)
app.command("draft")(assist.draft)


@app.command()
def init() -> None:
    """Create ~/.applyr, the database, and a default config."""
    state = get_state()
    console.print(f"database: {state.config.db_path}")
    console.print(f"config:   {state.config.home / 'config.toml'}")
    console.print("[green]ready[/green]")


@add_app.command("company")
def add_company(
    name: str,
    domain: str | None = typer.Option(None, help="e.g. stripe.com — used for email linking"),
    industry: str | None = None,
    size: str | None = None,
    hq: str | None = None,
    notes: str | None = None,
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Add a company."""
    state = get_state()
    with session_scope(state.engine) as session:
        action = AddCompany(
            company=NewCompany(
                name=name, domain=domain, industry=industry, size=size, hq=hq, notes=notes
            )
        )
        from applyr.cli.render import confirm_flow
        from applyr.llm.tools import ProposalView, ToolResult

        proposal = props.propose(session, action, source="cli")
        assert proposal.id is not None
        result = ToolResult(
            result="proposal_created",
            proposal=ProposalView(
                id=proposal.id,
                kind="add_company",
                diff=props.diff_for(session, proposal),
                auto_approvable=False,
            ),
        )
        confirm_flow(session, result, yes=yes)


@add_app.command("job")
def add_job(
    company: str = typer.Option(..., "--company", "-c"),
    title: str = typer.Option(..., "--title", "-t"),
    jd_file: str | None = typer.Option(None, help="Path to a file with the pasted JD text"),
    jd_url: str | None = typer.Option(None, help="URL to fetch the JD from (plain GET)"),
    url: str | None = typer.Option(None, help="Posting URL kept for the record"),
    source: str | None = typer.Option(None, help="linkedin|company_site|aggregator|other"),
    location: str | None = None,
    remote_policy: str | None = typer.Option(None, "--remote", help="onsite|hybrid|remote"),
    level: str | None = None,
    comp_min: int | None = None,
    comp_max: int | None = None,
    currency: str | None = None,
    posted_at: str | None = typer.Option(None, "--posted", help="YYYY-MM-DD"),
    save: bool = typer.Option(True, "--save/--no-save", help="Also save to the pipeline"),
) -> None:
    """Capture a job posting (the JD is archived immutably at capture time)."""
    jd_text: str | None = None
    if jd_file:
        from pathlib import Path

        jd_text = Path(jd_file).read_text(encoding="utf-8", errors="replace")
    state = get_state()
    with session_scope(state.engine) as session:
        dispatch_interactive(
            state,
            session,
            "add_job",
            {
                "company": company,
                "title": title,
                "jd_text": jd_text,
                "jd_url": jd_url,
                "url": url,
                "source": source,
                "location": location,
                "remote_policy": remote_policy,
                "level": level,
                "comp_min": comp_min,
                "comp_max": comp_max,
                "currency": currency,
                "posted_at": posted_at,
                "save": save,
            },
        )


@add_app.command("contact")
def add_contact(
    name: str,
    company: str | None = None,
    title: str | None = None,
    email: str | None = None,
    linkedin: str | None = None,
    relationship: str | None = typer.Option(None, help="recruiter|referrer|interviewer|peer"),
    notes: str | None = None,
) -> None:
    """Add a contact."""
    state = get_state()
    with session_scope(state.engine) as session:
        dispatch_interactive(
            state,
            session,
            "add_contact",
            {
                "name": name,
                "company": company,
                "title": title,
                "email": email,
                "linkedin": linkedin,
                "relationship": relationship,
                "notes": notes,
            },
        )


@add_app.command("doc")
def add_doc(
    path: str,
    label: str = typer.Option(..., "--label", "-l", help="e.g. resume-v3-backend"),
    doc_type: str = typer.Option("resume", "--type", help="resume|cover_letter|portfolio"),
) -> None:
    """Register a document (resume/cover letter) by content hash."""
    state = get_state()
    with session_scope(state.engine) as session:
        try:
            doc = documents_repo.register(session, path, DocumentType(doc_type), label)
        except documents_repo.DocumentError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        extracted = "yes" if doc.extracted_text else "no"
        console.print(
            f"[green]doc#{doc.id}[/green] {doc.label} ({doc.type}) "
            f"sha256 {doc.content_hash[:12]} · text extracted: {extracted}"
        )


@add_app.command("task")
def add_task(
    description: str,
    due: str = typer.Option(..., "--due", help="YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (UTC)"),
    application: str | None = typer.Option(None, "--app"),
    kind: str = typer.Option("follow_up", help="follow_up|prep|thank_you|deadline|other"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Add a follow-up/prep task."""
    due_at = due if " " in due or "T" in due else f"{due}T12:00:00"
    state = get_state()
    with session_scope(state.engine) as session:
        dispatch_interactive(
            state,
            session,
            "add_task",
            {
                "description": description,
                "due_at": due_at,
                "application": application,
                "kind": kind,
            },
            yes=yes,
        )


@app.command()
def apply(
    job: str = typer.Argument(help="job#id or free text like 'stripe backend'"),
    resume: str | None = typer.Option(None, help="Document label, doc#id, or file path"),
    cover_letter: str | None = typer.Option(None, "--cover"),
    date: str | None = typer.Option(None, help="YYYY-MM-DD, defaults to today"),
    source: str | None = typer.Option(None, help="referral|direct|easy_apply|recruiter"),
    referral: str | None = typer.Option(None, help="Referring contact"),
    priority: int | None = None,
) -> None:
    """Record that you applied to a job (pins the exact resume version)."""
    state = get_state()
    with session_scope(state.engine) as session:
        dispatch_interactive(
            state,
            session,
            "log_application",
            {
                "job": job,
                "applied_at": date,
                "resume": resume,
                "cover_letter": cover_letter,
                "source": source,
                "referral_contact": referral,
                "priority": priority,
            },
        )


@app.command()
def status(
    application: str = typer.Argument(help="app#id or free text"),
    to_status: Status = typer.Argument(help="new status"),
    date: str | None = typer.Option(None, help="When it happened (ISO, UTC)"),
    note: str | None = None,
) -> None:
    """Move an application to a new status (always confirms)."""
    state = get_state()
    with session_scope(state.engine) as session:
        dispatch_interactive(
            state,
            session,
            "update_status",
            {
                "application": application,
                "to_status": to_status.value,
                "occurred_at": date,
                "note": note,
            },
        )


@app.command()
def note(
    application: str,
    text: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation (safe action)"),
) -> None:
    """Attach a note to an application."""
    state = get_state()
    with session_scope(state.engine) as session:
        dispatch_interactive(
            state, session, "add_note", {"application": application, "text": text}, yes=yes
        )


@app.command("list")
def list_cmd(
    status_filter: str | None = typer.Option(None, "--status", "-s"),
    stale: bool = typer.Option(False, "--stale", help="Only stale (past SLA)"),
    ghosted: bool = typer.Option(False, "--ghosted", help="Only ghosted"),
    include_archived: bool = typer.Option(False, "--all"),
) -> None:
    """List applications with derived status."""
    state = get_state()
    with session_scope(state.engine) as session:
        apps = apps_repo.non_archived(session)
        if include_archived:
            from sqlmodel import select

            from applyr.core.models import Application

            apps = list(session.exec(select(Application)).all())
        table = Table(show_header=True, header_style="bold")
        for column in ("Ref", "Application", "Status", "In stage", "Quiet", "Source", "Flags"):
            table.add_column(column)
        shown = 0
        for application in apps:
            assert application.id is not None
            app_status = derived_status(session, application.id)
            status_value = app_status.value if app_status else "-"
            if status_filter and status_value != status_filter:
                continue
            app_stale = is_stale(session, application.id, state.config.sla_days)
            app_ghosted = is_ghosted(session, application.id, state.config.sla_days)
            if stale and not app_stale:
                continue
            if ghosted and not app_ghosted:
                continue
            flags = []
            if app_ghosted:
                flags.append("[red]ghosted[/red]")
            elif app_stale:
                flags.append("[yellow]stale[/yellow]")
            if application.archived:
                flags.append("[dim]archived[/dim]")
            days = days_in_stage(session, application.id)
            quiet = days_since_activity(session, application.id)
            table.add_row(
                f"app#{application.id}",
                apps_repo.label(session, application).rsplit(" (", 1)[0],
                status_value,
                f"{days}d" if days is not None else "-",
                f"{quiet}d" if quiet is not None else "-",
                application.source or "-",
                " ".join(flags),
            )
            shown += 1
        console.print(table)
        console.print(f"[dim]{shown} application(s)[/dim]")


@app.command()
def show(ref: str) -> None:
    """Full detail for one entity (app#12, job#3, co#1, ... or free text)."""
    state = get_state()
    with session_scope(state.engine) as session:
        ctx = ToolContext(session=session, engine=state.engine, config=state.config, source="cli")
        result = dispatch(ctx, "show", {"ref": ref})
        if result.result == "error":
            console.print(f"[red]{result.message}[/red]")
            raise typer.Exit(1)
        if result.result == "needs_disambiguation":
            from applyr.cli.render import print_candidates

            print_candidates(result)
            raise typer.Exit(1)
        data = result.data or {}
        if "events" in data:  # application detail gets a proper layout
            header = f"{data['company']} — {data['title']}  \\[{data['ref']}]"
            body = [
                f"status: [bold]{data['status']}[/bold] ({data['days_in_stage']}d in stage)",
                f"applied: {data['applied_at'] or '-'} · source: {data['source'] or '-'}"
                f" · resume: {data['resume'] or '-'}",
                f"JD archived: {'yes' if data['jd_archived'] else 'no'}"
                f" · linked emails: {data['linked_emails']}"
                f" · last activity: {data['last_activity'] or '-'}",
            ]
            console.print(Panel("\n".join(body), title=header, expand=False))
            if data["events"]:
                table = Table(title="events", show_header=True, header_style="bold")
                for column in ("When (UTC)", "Type", "Change", "Source", "Note"):
                    table.add_column(column)
                for ev in data["events"]:
                    change = ""
                    if ev["type"] == "status_change":
                        change = f"{ev['from'] or '·'} -> {ev['to']}"
                    table.add_row(
                        ev["occurred_at"], ev["type"], change, ev["source"], ev["note"] or ""
                    )
                console.print(table)
            for interview in data["interviews"]:
                console.print(
                    f"  interview {interview['ref']}: round {interview['round']} "
                    f"{interview['format']} @ {interview['scheduled_at']}"
                    + (f" — outcome: {interview['outcome']}" if interview["outcome"] else "")
                )
            for note_item in data["notes"]:
                console.print(f"  note {note_item['at']}: {note_item['text']}")
        else:
            print_ok(result)


@app.command()
def events(application: str) -> None:
    """Raw event timeline for an application."""
    state = get_state()
    with session_scope(state.engine) as session:
        resolved = res.resolve_application(session, application)
        if resolved.outcome != "resolved":
            console.print(f"[red]{resolved.hint or 'not resolved'}[/red]")
            raise typer.Exit(1)
        from sqlmodel import col, select

        stmt = (
            select(ApplicationEvent)
            .where(ApplicationEvent.application_id == resolved.entity_id)
            .order_by(col(ApplicationEvent.occurred_at), col(ApplicationEvent.id))
        )
        table = Table(show_header=True, header_style="bold")
        for column in ("id", "When (local)", "Type", "From", "To", "Source", "Payload"):
            table.add_column(column)
        for ev in session.exec(stmt).all():
            table.add_row(
                str(ev.id),
                to_local(ev.occurred_at).strftime("%Y-%m-%d %H:%M"),
                ev.type,
                ev.from_status or "",
                ev.to_status or "",
                ev.source,
                (ev.payload_json or "")[:60],
            )
        console.print(table)


@app.command()
def search(
    query: str,
    scope: str = typer.Option("all", help="all|companies|jobs|applications|contacts|emails|notes"),
    limit: int = 10,
) -> None:
    """Search everything (fuzzy + semantic when indexed)."""
    state = get_state()
    with session_scope(state.engine) as session:
        ctx = ToolContext(session=session, engine=state.engine, config=state.config, source="cli")
        result = dispatch(ctx, "search", {"query": query, "scope": scope, "limit": limit})
        print_ok(result)


@app.command()
def review() -> None:
    """Review pending proposals (email ingestion lands here). y/n/s/a/q."""
    state = get_state()
    with session_scope(state.engine) as session:
        queue = props.pending(session)
        if not queue:
            console.print("[dim]no pending proposals[/dim]")
            return
        approve_rest = False
        for i, proposal in enumerate(queue, start=1):
            assert proposal.id is not None
            action = props.load_action(proposal)
            title = (
                f"\\[{i}/{len(queue)}] proposal #{proposal.id} — {action.kind} "
                f"(source: {proposal.source})"
            )
            body = props.diff_for(session, proposal)
            utterance = props.load_utterance(proposal)
            if utterance:
                body += f"\n[dim]{utterance}[/dim]"
            console.print(Panel(body, title=title, expand=False))
            if approve_rest:
                choice = "y"
            else:
                choice = typer.prompt("[y]es / [n]o / [s]kip / [a]ll-yes / [q]uit", default="s")
                choice = choice.strip().casefold()
            if choice == "q":
                break
            if choice == "a":
                approve_rest = True
                choice = "y"
            if choice == "y":
                try:
                    result = props.confirm(session, proposal.id)
                    session.commit()
                    console.print(f"[green]{result.summary}[/green]")
                except (ProposalError, CommitError) as exc:
                    session.rollback()
                    console.print(f"[red]{exc}[/red]")
            elif choice == "n":
                props.reject(session, proposal.id, "rejected in review")
                session.commit()
                console.print("[yellow]rejected[/yellow]")


@app.command()
def archive(application: str, undo: bool = typer.Option(False, "--undo")) -> None:
    """Archive an application (bookkeeping flag; events are kept)."""
    state = get_state()
    with session_scope(state.engine) as session:
        resolved = res.resolve_application(session, application)
        if resolved.outcome != "resolved" or resolved.entity_id is None:
            console.print(f"[red]{resolved.hint or 'not resolved'}[/red]")
            raise typer.Exit(1)
        app_row = apps_repo.get(session, resolved.entity_id)
        assert app_row is not None
        app_row.archived = not undo
        session.add(app_row)
        console.print(
            f"{apps_repo.label(session, app_row)} "
            + ("[yellow]archived[/yellow]" if not undo else "[green]restored[/green]")
        )


@app.command()
def proposals(
    status_filter: str = typer.Option("pending", "--status", help="pending|accepted|rejected"),
) -> None:
    """List proposals."""
    state = get_state()
    with session_scope(state.engine) as session:
        ctx = ToolContext(session=session, engine=state.engine, config=state.config, source="cli")
        result = dispatch(ctx, "list_proposals", {"status": status_filter})
        for item in (result.data or {}).get("proposals", []):
            line = f"#{item['id']} {item['kind']} ({item['source']}, {item['created_at']})"
            if item.get("utterance"):
                line += f" — {item['utterance'][:70]}"
            console.print(line)
            if item.get("diff"):
                console.print(f"[dim]{item['diff']}[/dim]")


if __name__ == "__main__":
    app()
