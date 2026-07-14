"""Email ingestion commands: setup (keyring), poll (read-only IMAP + classify)."""

from __future__ import annotations

import re

import typer

from jobtrack.cli.context import get_state
from jobtrack.cli.render import console
from jobtrack.core.db import session_scope
from jobtrack.ingest import imap_poller
from jobtrack.ingest.linker import process_new
from jobtrack.llm.provider import ProviderError
from jobtrack.llm.router import provider_for

email_app = typer.Typer(no_args_is_help=True, help="Read-only email ingestion.")


def _write_config_user(config_path: str, user: str) -> bool:
    from pathlib import Path

    path = Path(config_path)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"(\[email\][^\[]*?user\s*=\s*\")[^\"]*(\")",
        lambda m: m.group(1) + user + m.group(2),
        text,
        count=1,
        flags=re.DOTALL,
    )
    if n == 0:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


@email_app.command()
def setup(
    user: str = typer.Option(..., "--user", prompt="IMAP account (email address)"),
    host: str | None = typer.Option(None, help="IMAP host (default from config: imap.gmail.com)"),
) -> None:
    """Store IMAP credentials in the OS keyring (never in config or the DB)."""
    state = get_state()
    password = typer.prompt(
        "IMAP password (for Gmail: an app password, https://myaccount.google.com/apppasswords)",
        hide_input=True,
    )
    imap_poller.store_password(user, password)
    config_path = str(state.config.home / "config.toml")
    if _write_config_user(config_path, user):
        console.print(f"[green]saved[/green] — account {user} written to {config_path}")
    else:
        console.print(
            f"[yellow]password stored, but set `user = \"{user}\"` under [email] in "
            f"{config_path} yourself[/yellow]"
        )
    if host:
        console.print(f"[yellow]also set `host = \"{host}\"` under [email][/yellow]")
    console.print("[dim]jobtrack only ever reads mail — it never sends, marks or deletes.[/dim]")


@email_app.command()
def poll() -> None:
    """Fetch new mail, classify locally, and queue proposals for review."""
    state = get_state()
    with session_scope(state.engine) as session:
        try:
            fetched = imap_poller.poll(session, state.config)
        except imap_poller.IngestError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        except Exception as exc:  # IMAP/network errors: report, don't traceback
            console.print(f"[red]IMAP error: {exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(f"fetched {fetched} new email(s)")
        session.commit()

        try:
            provider = provider_for(state.config, "classify")
            created = process_new(session, provider, state.config)
        except ProviderError as exc:
            console.print(
                f"[yellow]stored, but classification skipped: {exc} — "
                "run `jobtrack email poll` again once Ollama is up[/yellow]"
            )
            return
        if created:
            console.print(
                f"[bold]{len(created)} proposal(s) queued[/bold] — run `jobtrack review`"
            )
        else:
            console.print("[dim]no actionable job mail this time[/dim]")
