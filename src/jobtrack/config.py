"""Configuration: ~/.jobtrack/config.toml with defaults, overridable via JOBTRACK_HOME."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SLA_DAYS: dict[str, int] = {
    "applied": 14,
    "screening": 10,
    "assessment": 7,
    "interviewing": 10,
    "final_round": 7,
    "offer": 5,
}

DEFAULT_TASKS: dict[str, str] = {
    "classify": "local",
    "jd_parse": "local",
    "prep": "local",
    "draft": "local",
    "say": "local",
}

DEFAULT_CONFIG_TOML = """\
# jobtrack configuration. Everything runs locally by default.

[llm]
provider = "ollama"
base_url = "http://localhost:11434"
chat_model = "qwen3:8b"
embed_model = "nomic-embed-text"
# Only used for tasks routed to "anthropic" below (requires `uv sync --extra anthropic`
# and ANTHROPIC_API_KEY in the environment). Default routing is fully local.
anthropic_model = "claude-sonnet-5"

[llm.tasks]
# Route individual tasks: "local" or "anthropic".
classify = "local"
jd_parse = "local"
prep = "local"
draft = "local"
say = "local"

[email]
host = "imap.gmail.com"
user = ""
folder = "INBOX"
# Classifications at or above this confidence become pending proposals.
confidence_threshold = 0.75

[sla_days]
# Days without activity before an application counts as stale/ghosted, per stage.
applied = 14
screening = 10
assessment = 7
interviewing = 10
final_round = 7
offer = 5

[approvals]
# Action kinds eligible for --yes / auto-approve. Status changes and new
# applications always require confirmation regardless of this list.
auto = ["add_note", "log_interaction"]
"""


def jobtrack_home() -> Path:
    return Path(os.environ.get("JOBTRACK_HOME", str(Path.home() / ".jobtrack")))


@dataclass
class LLMConfig:
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    chat_model: str = "qwen3:8b"
    embed_model: str = "nomic-embed-text"
    anthropic_model: str = "claude-sonnet-5"
    tasks: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TASKS))


@dataclass
class EmailConfig:
    host: str = "imap.gmail.com"
    user: str = ""
    folder: str = "INBOX"
    confidence_threshold: float = 0.75


@dataclass
class Config:
    home: Path
    llm: LLMConfig = field(default_factory=LLMConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    sla_days: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_SLA_DAYS))
    auto_approve: tuple[str, ...] = ("add_note", "log_interaction")

    @property
    def db_path(self) -> Path:
        return self.home / "jobtrack.db"

    @property
    def snapshots_dir(self) -> Path:
        return self.home / "snapshots"

    @property
    def prep_dir(self) -> Path:
        return self.home / "prep"


def load_config() -> Config:
    home = jobtrack_home()
    path = home / "config.toml"
    data: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))

    llm_data = data.get("llm", {})
    llm = LLMConfig(
        provider=llm_data.get("provider", "ollama"),
        base_url=llm_data.get("base_url", "http://localhost:11434"),
        chat_model=llm_data.get("chat_model", "qwen3:8b"),
        embed_model=llm_data.get("embed_model", "nomic-embed-text"),
        anthropic_model=llm_data.get("anthropic_model", "claude-sonnet-5"),
        tasks={**DEFAULT_TASKS, **llm_data.get("tasks", {})},
    )
    email_data = data.get("email", {})
    email = EmailConfig(
        host=email_data.get("host", "imap.gmail.com"),
        user=email_data.get("user", ""),
        folder=email_data.get("folder", "INBOX"),
        confidence_threshold=float(email_data.get("confidence_threshold", 0.75)),
    )
    sla = {**DEFAULT_SLA_DAYS, **data.get("sla_days", {})}
    auto = tuple(data.get("approvals", {}).get("auto", ["add_note", "log_interaction"]))
    return Config(home=home, llm=llm, email=email, sla_days=sla, auto_approve=auto)


def ensure_home(config: Config) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    config.snapshots_dir.mkdir(parents=True, exist_ok=True)
    config.prep_dir.mkdir(parents=True, exist_ok=True)
    config_path = config.home / "config.toml"
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
